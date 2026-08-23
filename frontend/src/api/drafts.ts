const DATABASE_NAME = "nanocat-web";
const STORE_NAME = "draft-attachments";
const METADATA_STORE_NAME = "draft-attachment-metadata";
const UPDATED_AT_INDEX = "updatedAt";
const DATABASE_VERSION = 2;
const MAX_FILES = 16;
const MAX_TOTAL_BYTES = 64 * 1024 * 1024;
const MAX_DRAFT_RECORDS = 128;
const DRAFT_TTL_MS = 7 * 24 * 60 * 60 * 1000;

interface StoredFile {
  name: string;
  type: string;
  lastModified: number;
  bytes?: ArrayBuffer;
  blob?: Blob;
}

interface DraftRecord {
  sessionId: string;
  files: StoredFile[];
  updatedAt: number;
}

interface DraftMetadata {
  sessionId: string;
  updatedAt: number;
}

function openDatabase(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DATABASE_NAME, DATABASE_VERSION);
    request.onupgradeneeded = (event) => {
      const database = request.result;
      const transaction = request.transaction;
      let drafts: IDBObjectStore;
      if (!database.objectStoreNames.contains(STORE_NAME)) {
        drafts = database.createObjectStore(STORE_NAME, { keyPath: "sessionId" });
      } else {
        drafts = transaction!.objectStore(STORE_NAME);
      }
      let metadata: IDBObjectStore;
      if (!database.objectStoreNames.contains(METADATA_STORE_NAME)) {
        metadata = database.createObjectStore(METADATA_STORE_NAME, { keyPath: "sessionId" });
        metadata.createIndex(UPDATED_AT_INDEX, UPDATED_AT_INDEX);
      } else {
        metadata = transaction!.objectStore(METADATA_STORE_NAME);
      }
      if ((event as IDBVersionChangeEvent).oldVersion < 2) {
        const migratedAt = Date.now();
        const cursor = drafts.openCursor();
        cursor.onsuccess = () => {
          const current = cursor.result;
          if (!current) return;
          const record = current.value as Partial<DraftRecord>;
          metadata.put({
            sessionId: String(record.sessionId ?? current.primaryKey),
            updatedAt: typeof record.updatedAt === "number" ? record.updatedAt : migratedAt,
          } satisfies DraftMetadata);
          current.continue();
        };
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("Draft database could not be opened"));
    request.onblocked = () => reject(new Error("Draft database upgrade is blocked"));
  });
}

async function runRequest<T>(mode: IDBTransactionMode, action: (store: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  const database = await openDatabase();
  try {
    return await new Promise<T>((resolve, reject) => {
      const transaction = database.transaction(STORE_NAME, mode);
      const request = action(transaction.objectStore(STORE_NAME));
      let result!: T;
      request.onsuccess = () => { result = request.result; };
      request.onerror = () => reject(request.error ?? new Error("Draft attachment operation failed"));
      transaction.onabort = () => reject(transaction.error ?? new Error("Draft attachment transaction was aborted"));
      transaction.onerror = () => reject(transaction.error ?? new Error("Draft attachment transaction failed"));
      transaction.oncomplete = () => resolve(result);
    });
  } finally {
    database.close();
  }
}

export async function loadDraftFiles(sessionId: string): Promise<File[]> {
  const record = await runRequest<DraftRecord | undefined>("readonly", (store) => store.get(sessionId));
  return (record?.files ?? []).map((item) => new File([
    item.bytes ?? item.blob ?? new ArrayBuffer(0),
  ], item.name, {
    type: item.type,
    lastModified: item.lastModified,
  }));
}

export async function saveDraftFiles(sessionId: string, files: File[]): Promise<void> {
  validateDraftFiles(files);
  if (!files.length) {
    await deleteDraftFiles(sessionId);
    return;
  }
  const updatedAt = Date.now();
  const storedFiles = await Promise.all(files.map(async (file) => ({
    name: file.name,
    type: file.type,
    lastModified: file.lastModified,
    bytes: await file.arrayBuffer(),
  })));
  const record: DraftRecord = {
    sessionId,
    updatedAt,
    files: storedFiles,
  };
  const database = await openDatabase();
  try {
    await new Promise<void>((resolve, reject) => {
      const transaction = database.transaction(
        [STORE_NAME, METADATA_STORE_NAME],
        "readwrite",
      );
      const draftRequest = transaction.objectStore(STORE_NAME).put(record);
      const metadataRequest = transaction.objectStore(METADATA_STORE_NAME).put({ sessionId, updatedAt } satisfies DraftMetadata);
      draftRequest.onerror = () => reject(draftRequest.error ?? new Error("Draft attachment record could not be saved"));
      metadataRequest.onerror = () => reject(metadataRequest.error ?? new Error("Draft attachment metadata could not be saved"));
      transaction.onabort = () => reject(transaction.error ?? new Error("Draft attachment save was aborted"));
      transaction.onerror = () => reject(transaction.error ?? new Error("Draft attachment save failed"));
      transaction.oncomplete = () => resolve();
    });
  } finally {
    database.close();
  }
  await pruneDraftFiles();
}

export async function deleteDraftFiles(sessionId: string): Promise<void> {
  const database = await openDatabase();
  try {
    await new Promise<void>((resolve, reject) => {
      const transaction = database.transaction(
        [STORE_NAME, METADATA_STORE_NAME],
        "readwrite",
      );
      transaction.objectStore(STORE_NAME).delete(sessionId);
      transaction.objectStore(METADATA_STORE_NAME).delete(sessionId);
      transaction.onabort = () => reject(transaction.error ?? new Error("Draft attachment deletion was aborted"));
      transaction.onerror = () => reject(transaction.error ?? new Error("Draft attachment deletion failed"));
      transaction.oncomplete = () => resolve();
    });
  } finally {
    database.close();
  }
}

export function validateDraftFiles(files: File[]): void {
  if (files.length > MAX_FILES || files.reduce((total, file) => total + file.size, 0) > MAX_TOTAL_BYTES) {
    throw new Error("Draft attachments exceed the 16-file or 64 MiB limit");
  }
}

export async function pruneDraftFiles(): Promise<void> {
  const database = await openDatabase();
  try {
    await new Promise<void>((resolve, reject) => {
      const transaction = database.transaction(
        [STORE_NAME, METADATA_STORE_NAME],
        "readwrite",
      );
      const drafts = transaction.objectStore(STORE_NAME);
      const metadata = transaction.objectStore(METADATA_STORE_NAME);
      const request = metadata.index(UPDATED_AT_INDEX).openCursor(null, "prev");
      let retained = 0;
      const now = Date.now();
      request.onsuccess = () => {
        const cursor = request.result;
        if (!cursor) return;
        const record = cursor.value as DraftMetadata;
        const expired = record.updatedAt <= 0 || now - record.updatedAt > DRAFT_TTL_MS;
        if (expired || retained >= MAX_DRAFT_RECORDS) {
          drafts.delete(record.sessionId);
          cursor.delete();
        } else {
          retained += 1;
        }
        cursor.continue();
      };
      request.onerror = () => reject(request.error ?? new Error("Draft cleanup could not read metadata"));
      transaction.onabort = () => reject(transaction.error ?? new Error("Draft cleanup was aborted"));
      transaction.onerror = () => reject(transaction.error ?? new Error("Draft cleanup failed"));
      transaction.oncomplete = () => resolve();
    });
  } finally {
    database.close();
  }
}
