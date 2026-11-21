export type ParseResult<T> =
  | { ok: true; value: T }
  | { ok: false; error: string };

const INT_RE = /^[+-]?\d+$/;
const FLOAT_RE = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/;

export function parseStrictInt(
  raw: string,
  opts: { label: string; min: number; max: number; optional: true },
): ParseResult<number | undefined>;
export function parseStrictInt(
  raw: string,
  opts: { label: string; min: number; max: number; optional?: false },
): ParseResult<number>;
export function parseStrictInt(
  raw: string,
  opts: { label: string; min: number; max: number; optional?: boolean },
): ParseResult<number | undefined> {
  const { label, min, max, optional = false } = opts;
  const trimmed = raw.trim();
  if (!trimmed) {
    return optional
      ? { ok: true, value: undefined }
      : { ok: false, error: `${label}不能为空（需为 ${min}..${max} 之间的整数）` };
  }
  if (!INT_RE.test(trimmed)) {
    return { ok: false, error: `${label}必须为整数（${min}..${max}）` };
  }
  const value = Number(trimmed);
  if (!Number.isSafeInteger(value)) {
    return { ok: false, error: `${label}数值超出整数范围` };
  }
  if (value < min || value > max) {
    return { ok: false, error: `${label}需在 ${min}..${max} 之间` };
  }
  return { ok: true, value: value === 0 ? 0 : value };
}

export function parseStrictFloat(
  raw: string,
  opts: { label: string; min?: number; max?: number; optional: true },
): ParseResult<number | undefined>;
export function parseStrictFloat(
  raw: string,
  opts: { label: string; min?: number; max?: number; optional?: false },
): ParseResult<number>;
export function parseStrictFloat(
  raw: string,
  opts: { label: string; min?: number; max?: number; optional?: boolean },
): ParseResult<number | undefined> {
  const { label, min, max, optional = false } = opts;
  const trimmed = raw.trim();
  if (!trimmed) {
    return optional
      ? { ok: true, value: undefined }
      : { ok: false, error: `${label}不能为空` };
  }
  if (!FLOAT_RE.test(trimmed)) {
    return { ok: false, error: `${label}必须为有效数字` };
  }
  const value = Number(trimmed);
  if (!Number.isFinite(value)) {
    return { ok: false, error: `${label}必须为有限数字` };
  }
  if (min !== undefined && value < min) {
    return {
      ok: false,
      error: max !== undefined ? `${label}需在 ${min}..${max} 之间` : `${label}不能小于 ${min}`,
    };
  }
  if (max !== undefined && value > max) {
    return {
      ok: false,
      error: min !== undefined ? `${label}需在 ${min}..${max} 之间` : `${label}不能大于 ${max}`,
    };
  }
  return { ok: true, value: value === 0 ? 0 : value };
}
