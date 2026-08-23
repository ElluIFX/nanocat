import type { ComponentChildren, JSX } from "preact";
import { AlertTriangle, Check, Circle, LoaderCircle, XCircle } from "lucide-preact";
import type { RunStatus } from "../types";

export function StatusBadge({ status, label }: { status: RunStatus; label?: string }) {
  const icon = status === "completed"
    ? <Check size={12} />
    : status === "failed" || status === "cancelled"
      ? <XCircle size={12} />
      : status === "waiting_approval" || status === "waiting_user"
        ? <AlertTriangle size={12} />
        : status === "running" || status === "queued" || status === "cancelling"
          ? <LoaderCircle class="spin" size={12} />
          : <Circle size={10} />;
  return <span class={`status-badge status-${status}`}>{icon}{label ?? status.replaceAll("_", " ")}</span>;
}

export function IconButton({ label, children, class: className = "", ...props }: {
  label: string;
  children: ComponentChildren;
  class?: string;
} & Omit<JSX.IntrinsicElements["button"], "aria-label" | "class">) {
  return <button type="button" class={`icon-button ${className}`} aria-label={label} title={label} {...props}>{children}</button>;
}

export function EmptyState({ icon, title, description, action }: {
  icon: ComponentChildren;
  title: string;
  description: string;
  action?: ComponentChildren;
}) {
  return <div class="empty-state">
    <div class="empty-icon" aria-hidden="true">{icon}</div>
    <h2>{title}</h2>
    <p>{description}</p>
    {action}
  </div>;
}

export function Skeleton({ lines = 3 }: { lines?: number }) {
  return <div class="skeleton" aria-label="Loading" aria-busy="true">
    {Array.from({ length: lines }, (_, index) => <span key={index} style={{ width: `${86 - index * 11}%` }} />)}
  </div>;
}

export function Modal({ title, children, onClose }: {
  title: string;
  children: ComponentChildren;
  onClose: () => void;
}) {
  return <div class="modal-backdrop" role="presentation" onMouseDown={(event) => {
    if (event.currentTarget === event.target) onClose();
  }}>
    <section class="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title">
      <header><h2 id="modal-title">{title}</h2><IconButton label="Close" onClick={onClose}>×</IconButton></header>
      {children}
    </section>
  </div>;
}
