import type { ReactNode } from "react";
import { money, num, signClass } from "@/lib/format";

export function Panel({
  title,
  children,
  flush,
  actions,
}: {
  title: string;
  children: ReactNode;
  flush?: boolean;
  actions?: ReactNode;
}) {
  return (
    <section className="panel">
      <h3 style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span>{title}</span>
        {actions}
      </h3>
      <div className={flush ? "panel-body flush" : "panel-body"}>{children}</div>
    </section>
  );
}

export function Stat({
  label,
  value,
  sub,
  tone,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  tone?: string;
}) {
  return (
    <div className="panel">
      <div className="stat">
        <div className="label">{label}</div>
        <div className={`value ${tone ?? ""}`}>{value}</div>
        {sub ? <div className="sub">{sub}</div> : null}
      </div>
    </div>
  );
}

export function MoneyStat({
  label,
  value,
  currency = "USD",
  sub,
}: {
  label: string;
  value: string | number;
  currency?: string;
  sub?: ReactNode;
}) {
  return (
    <Stat
      label={label}
      value={money(value, currency)}
      sub={sub}
      tone={signClass(value)}
    />
  );
}

export function RiskBadge({ level }: { level: string }) {
  return <span className={`badge level-${level}`}>{level.replace("_", " ")}</span>;
}

export function StateBadge({ state }: { state: string }) {
  return <span className={`state state-${state}`}>{state}</span>;
}

export function ConnectionDot({ connected }: { connected: boolean }) {
  return <span className={`dot ${connected ? "on" : "off"}`} />;
}

export function Bar({ pct, warnAt = 70, dangerAt = 90 }: { pct: number; warnAt?: number; dangerAt?: number }) {
  const clamped = Math.max(0, Math.min(100, pct));
  const tone = clamped >= dangerAt ? "danger" : clamped >= warnAt ? "warn" : "";
  return (
    <div className={`bar ${tone}`}>
      <span style={{ width: `${clamped}%` }} />
    </div>
  );
}

export function ErrorBox({ message }: { message: string | null }) {
  if (!message) return null;
  return <div className="error">{message}</div>;
}

export function Notice({ children }: { children: ReactNode }) {
  return <div className="notice">{children}</div>;
}

export function WarnNote({ children }: { children: ReactNode }) {
  return <div className="warn-note">{children}</div>;
}

export function Loading({ what = "data" }: { what?: string }) {
  return <div className="spinner">loading {what}…</div>;
}

export function Empty({ what }: { what: string }) {
  return <div className="spinner">no {what} yet</div>;
}

export function Signed({ value, currency = "USD" }: { value: string; currency?: string }) {
  return <span className={signClass(value)}>{money(value, currency)}</span>;
}

export function Delta({ value, digits = 6 }: { value: string; digits?: number }) {
  return <span className={signClass(value)}>{num(value).toFixed(digits)}</span>;
}
