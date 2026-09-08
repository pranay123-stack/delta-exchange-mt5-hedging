/**
 * Display formatting.
 *
 * All values arrive as strings. Conversion to `number` happens here and only
 * here, purely for presentation -- never for arithmetic that feeds back into
 * an order.
 */

export function num(value: string | number | null | undefined): number {
  if (value === null || value === undefined || value === "") return 0;
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

export function money(
  value: string | number | null | undefined,
  currency = "USD",
  digits = 2,
): string {
  const parsed = num(value);
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency,
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(parsed);
}

export function decimal(
  value: string | number | null | undefined,
  digits = 4,
): string {
  return num(value).toLocaleString("en-US", {
    minimumFractionDigits: 0,
    maximumFractionDigits: digits,
  });
}

export function bps(value: string | number | null | undefined, digits = 2): string {
  return `${num(value).toFixed(digits)} bps`;
}

export function pct(value: string | number | null | undefined, digits = 2): string {
  return `${num(value).toFixed(digits)}%`;
}

export function ratio(value: string | number | null | undefined): string {
  return num(value).toFixed(6);
}

/** Keep a raw exchange quantity readable without losing meaningful digits. */
export function quantity(value: string | null | undefined): string {
  if (!value) return "0";
  const trimmed = value.includes(".")
    ? value.replace(/0+$/, "").replace(/\.$/, "")
    : value;
  return trimmed === "" || trimmed === "-" ? "0" : trimmed;
}

export function time(value: string | null | undefined): string {
  if (!value) return "-";
  return new Date(value).toLocaleTimeString("en-GB", { hour12: false });
}

export function datetime(value: string | null | undefined): string {
  if (!value) return "-";
  return new Date(value).toLocaleString("en-GB", { hour12: false });
}

export function signClass(value: string | number | null | undefined): string {
  const parsed = num(value);
  if (parsed > 0) return "pos";
  if (parsed < 0) return "neg";
  return "";
}

/** Margin level is a percentage where a *large* number is healthy. */
export function marginLevelClass(value: string | number | null | undefined): string {
  const parsed = num(value);
  if (parsed >= 99_999) return "";
  if (parsed <= 100) return "level-KILL_SWITCH";
  if (parsed <= 120) return "level-EMERGENCY";
  if (parsed <= 150) return "level-DANGER";
  if (parsed <= 200) return "level-WARNING";
  return "level-NORMAL";
}
