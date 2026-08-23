import { Check, ChevronDown, ChevronRight, LockKeyhole, LogOut, Plus, RefreshCw, RotateCcw, Save, ShieldAlert, Trash2 } from "lucide-preact";
import { useEffect, useMemo, useState } from "preact/hooks";
import { api } from "../api/client";
import { hrefFor, route } from "../router";
import { applyTheme, loadModels, loadSettings, models, reportOperationError, runtime, settings, settingsDegradedReason, settingsRevision, settingsWritable, theme } from "../store";
import type { ModelInfo, SettingField, SettingGroup, SettingSection, SettingsUpdateResult } from "../types";
import { PageHeader } from "../components/AppShell";

const descriptions: Record<string, string> = {
  general: "Appearance and configuration format", agents: "Agent defaults, models, context, and compaction",
  runtime: "Runtime limits and execution behavior", runtimeFiles: "Temporary runtime artifact storage and quotas",
  api: "Core HTTP API listener and authentication", channels: "Channel-specific connectivity and behavior",
  providers: "Provider credentials, endpoints, and model catalog", heartbeat: "Heartbeat scheduling and delivery",
  tools: "Tool availability, safety policy, MCP, and execution limits", transcription: "Speech-to-text provider settings",
  memory: "Nowledge, thread capture, retrieval, and distillation",
};

const title = (value: string) => value.replace(/([a-z])([A-Z])/g, "$1 $2").replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase());

function groupFields(section: SettingSection): SettingGroup {
  const root: SettingGroup = { id: section.id, title: section.title, path: section.id, fields: [], groups: [] };
  for (const field of section.fields) {
    const parts = field.key.split(".");
    if (parts[0] === section.id || (section.id === "general" && parts[0] === "schemaVersion")) parts.shift();
    if (parts.length <= 1) { root.fields.push(field); continue; }
    let current = root;
    for (const part of parts.slice(0, -1)) {
      let child = current.groups.find((item) => item.id === part);
      if (!child) { child = { id: part, title: title(part), path: `${current.path}.${part}`, fields: [], groups: [] }; current.groups.push(child); }
      current = child;
    }
    current.fields.push(field);
  }
  return root;
}

function groupedModels(items: ModelInfo[]): Array<[string, ModelInfo[]]> {
  const groups = new Map<string, ModelInfo[]>();
  for (const model of items) { const label = model.providerLabel || model.provider || "Other"; groups.set(label, [...(groups.get(label) ?? []), model]); }
  return [...groups.entries()].sort(([leftLabel, left], [rightLabel, right]) => Number(right.some((item) => item.configured)) - Number(left.some((item) => item.configured)) || leftLabel.localeCompare(rightLabel));
}

export function ModelSelect({ value, disabled, onChange }: { value: unknown; disabled: boolean; onChange: (value: string) => void }) {
  const current = String(value ?? "");
  return <select value={current} disabled={disabled} onChange={(event) => onChange(event.currentTarget.value)}>
    {current && !models.value.some((model) => model.id === current) && <option value={current}>{current}</option>}
    {!current && <option value="">Use fallback</option>}
    {groupedModels(models.value).map(([provider, items]) => <optgroup key={provider} label={provider}>{items.map((model) => <option key={model.id} value={model.id}>{model.name || model.id}</option>)}</optgroup>)}
  </select>;
}

const applyLabel = (mode?: SettingField["applyMode"]) => mode === "restart" ? "Restart required" : mode === "reconnect" ? "Reconnects service" : mode === "next_turn" ? "Next turn" : null;

function Field({ field, value, onChange, disabled }: { field: SettingField; value: unknown; onChange: (value: unknown) => void; disabled?: boolean }) {
  const fieldDisabled = Boolean(disabled || field.readOnly);
  const isModel = /(?:^|\.)(?:model|subagentModel|assistantModel|visionModel|compactionModel)$/.test(field.key);
  const badge = applyLabel(field.applyMode);
  return <label class="setting-field">
    <span class="setting-copy"><strong>{field.label}{badge && <em class={`apply-mode ${field.applyMode}`}>{badge}</em>}{field.configured && <em>Configured</em>}{field.overriddenValue !== undefined && <em>Environment override</em>}</strong>{field.description && <small>{field.description}</small>}</span>
    {isModel ? <ModelSelect value={value} disabled={fieldDisabled} onChange={onChange} /> : field.type === "boolean"
      ? <input class="switch" type="checkbox" checked={Boolean(value)} disabled={fieldDisabled} onChange={(event) => onChange(event.currentTarget.checked)} />
      : field.type === "select" ? <select value={String(value ?? "")} disabled={fieldDisabled} onChange={(event) => onChange(event.currentTarget.value)}>{field.options?.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}</select>
        : field.type === "json" ? <span class="setting-json-row"><textarea rows={4} spellcheck={false} disabled={fieldDisabled} value={String(value ?? "")} placeholder={field.secret && field.configured ? "Configured · enter a complete JSON value to replace" : undefined} onInput={(event) => onChange(event.currentTarget.value)} />{field.secret && field.configured && !fieldDisabled && <button type="button" class="text-button" onClick={() => onChange("{}")}>Clear</button>}</span>
          : <span class="setting-input-row"><input type={field.secret ? "password" : field.type} min={field.minimum} max={field.maximum} disabled={fieldDisabled} value={String(value ?? "")} placeholder={field.secret && field.configured ? "Configured · enter to replace" : undefined} onInput={(event) => onChange(field.type === "number" ? Number(event.currentTarget.value) : event.currentTarget.value)} />{field.secret && field.configured && !fieldDisabled && <button type="button" class="text-button" onClick={() => onChange("")}>Clear</button>}</span>}
  </label>;
}

function SettingsGroup({ group, changes, setChanges, writable, depth = 0 }: { group: SettingGroup; changes: Record<string, unknown>; setChanges: (update: (current: Record<string, unknown>) => Record<string, unknown>) => void; writable: boolean; depth?: number }) {
  const visible = group.fields.filter((field) => !field.key.endsWith(".modelChoice"));
  if (!visible.length && !group.groups.length) return null;
  return <details class={`settings-tree-group depth-${depth}`} open={depth === 0 || /\.web$/.test(group.path)}>
    <summary><ChevronRight size={15} /><span>{group.title}</span><small>{visible.length + group.groups.length}</small></summary>
    <div class="settings-tree-body">{visible.map((field) => <Field key={field.key} field={field} disabled={!writable} value={Object.prototype.hasOwnProperty.call(changes, field.key) ? changes[field.key] : field.value} onChange={(value) => setChanges((current) => ({ ...current, [field.key]: value }))} />)}{group.groups.map((child) => <SettingsGroup key={child.path} group={child} changes={changes} setChanges={setChanges} writable={writable} depth={depth + 1} />)}</div>
  </details>;
}

function ModelCatalog() {
  const [provider, setProvider] = useState(""); const [model, setModel] = useState(""); const [working, setWorking] = useState("");
  const refresh = async () => { models.value = []; await loadModels(); };
  const add = async () => { if (!provider.trim() || !model.trim()) return; setWorking("add"); try { await api.addModel(provider.trim(), model.trim()); setProvider(""); setModel(""); await refresh(); } catch (error) { reportOperationError(error, "The model could not be added"); } finally { setWorking(""); } };
  const remove = async (item: ModelInfo) => { if (item.references?.length || item.removable === false || models.value.length <= 1) return; setWorking(item.id); try { await api.deleteModel(item.id); await refresh(); } catch (error) { reportOperationError(error, "The model could not be removed"); } finally { setWorking(""); } };
  return <section class="settings-group model-catalog"><h3>Model catalog</h3><div class="model-add-row"><input aria-label="Provider" placeholder="Provider" value={provider} onInput={(event) => setProvider(event.currentTarget.value)} /><input aria-label="Model ID" placeholder="Model ID" value={model} onInput={(event) => setModel(event.currentTarget.value)} /><button class="secondary-button" disabled={!provider.trim() || !model.trim() || Boolean(working)} onClick={() => void add()}><Plus size={15} /> Add</button></div><div class="model-catalog-list">{groupedModels(models.value).map(([providerLabel, items]) => <details open key={providerLabel}><summary><ChevronDown size={15} /><strong>{providerLabel}</strong><span>{items.length}</span></summary>{items.map((item) => { const blocked = models.value.length <= 1 || item.removable === false || Boolean(item.references?.length); return <div class="model-catalog-item" key={item.id}><span><strong>{item.name || item.id}</strong><small>{item.id}{item.references?.length ? ` · used by ${item.references.join(", ")}` : ""}</small></span><button aria-label={`Remove ${item.id}`} title={blocked ? "Assigned models and the final catalog entry cannot be removed" : "Remove model"} disabled={blocked || Boolean(working)} onClick={() => void remove(item)}><Trash2 size={14} /></button></div>; })}</details>)}</div></section>;
}

function resultSummary(result: SettingsUpdateResult): string {
  return [[result.appliedPaths, "applied"], [result.nextTurnPaths, "next turn"], [result.reconnectedPaths, "reconnected"], [result.restartRequiredPaths, "restart required"]].flatMap(([paths, label]) => Array.isArray(paths) && paths.length ? [`${paths.length} ${label}`] : []).join(" · ") || "applied";
}

export function Settings() {
  const requestedSectionId = route.value.name === "settings" ? route.value.section ?? "general" : "general";
  const currentSectionId = requestedSectionId === "web" ? "channels" : requestedSectionId;
  const sections = useMemo(() => { const loaded = settings.value.map((section) => ({ ...section, description: descriptions[section.id] ?? section.description })); return loaded.some((item) => item.id === "general") ? loaded : [{ id: "general", title: "General", description: descriptions.general, fields: [] }, ...loaded]; }, [settings.value]);
  const current = sections.find((item) => item.id === currentSectionId) ?? sections[0]; const grouped = useMemo(() => groupFields(current), [current]);
  const [changes, setChanges] = useState<Record<string, unknown>>({}); const [saved, setSaved] = useState(""); const [error, setError] = useState("");
  useEffect(() => { void Promise.all([loadSettings(), loadModels()]).catch((cause: Error) => setError(cause.message)); }, []);
  const save = async () => { setError(""); try { const fields = new Map(sections.flatMap((section) => section.fields).map((field) => [field.key, field])); const values = Object.fromEntries(Object.entries(changes).map(([key, value]) => { if (fields.get(key)?.type !== "json") return [key, value]; try { return [key, JSON.parse(String(value))]; } catch { throw new Error(`${fields.get(key)?.label ?? key} must contain valid JSON`); } })); const result = await api.updateSettings(values, settingsRevision.value); setChanges({}); setSaved(resultSummary(result)); window.setTimeout(() => setSaved(""), 3000); await loadSettings(); } catch (cause) { setError(cause instanceof Error ? cause.message : "Settings could not be saved"); } };
  return <div class="settings-page"><PageHeader title="Settings" eyebrow="Workspace preferences" actions={settingsWritable.value && Object.keys(changes).length > 0 && <><button class="secondary-button" onClick={() => setChanges({})}><RotateCcw size={15} /> Reset</button><button class="primary-button" onClick={() => void save()}><Save size={15} /> Save changes</button></>} /><div class="settings-layout"><nav class="settings-nav" aria-label="Settings sections">{sections.map((section) => <a key={section.id} data-router href={hrefFor({ name: "settings", section: section.id })} class={section.id === current.id ? "active" : ""} aria-current={section.id === current.id ? "page" : undefined}><span>{section.title}</span><ChevronRight size={15} /></a>)}</nav><main class="settings-content"><header><div><h2>{current.title}</h2><p>{current.description}</p></div></header>{error && <div class="inline-error" role="alert"><ShieldAlert size={16} />{error}</div>}{!settingsWritable.value && settingsDegradedReason.value && <div class="readonly-notice" role="status"><LockKeyhole size={16} /><span><strong>Read-only configuration</strong>{settingsDegradedReason.value}</span></div>}{current.id === "general" && <section class="settings-group"><h3>Appearance</h3><div class="theme-options" role="radiogroup" aria-label="Theme">{(["system", "light", "dark"] as const).map((item) => <button data-theme-option={item} role="radio" tabIndex={theme.value === item ? 0 : -1} aria-checked={theme.value === item} class={theme.value === item ? "active" : ""} onClick={() => applyTheme(item)}><span class={`theme-preview ${item}`} />{item}<Check size={15} /></button>)}</div></section>}{(grouped.fields.length || grouped.groups.length) ? <section class="settings-tree"><SettingsGroup group={grouped} changes={changes} setChanges={setChanges} writable={settingsWritable.value} /></section> : current.id !== "general" && <section class="settings-placeholder"><RefreshCw size={20} /><h3>Runtime configuration is loading</h3><p>This section will populate from the server's typed settings schema.</p></section>}{current.id === "providers" && <ModelCatalog />}{current.id === "channels" && <section class="settings-group danger-zone"><h3><LockKeyhole size={16} /> Browser sessions</h3>{runtime.value?.unprotected ? <p>This local Web instance has no password. Browser login sessions are inactive.</p> : <><p>Signing out revokes this browser session.</p><button class="secondary-button" onClick={() => void api.logout().then(() => window.location.assign("/login")).catch((cause) => reportOperationError(cause, "The browser session could not be revoked"))}><LogOut size={15} /> Sign out</button></>}</section>}</main></div>{saved && <div class="toast" role="status"><Check size={15} /> Settings saved · {saved}</div>}</div>;
}
