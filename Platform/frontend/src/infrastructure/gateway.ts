/**
 * Доступ к API gateway (инфраструктурный слой).
 * В dev Vite проксирует /api; в prod — same-origin через nginx.
 */
export function resolveGatewayBase(): string {
  if (import.meta.env.DEV) {
    return "";
  }
  const v = import.meta.env.VITE_GATEWAY_BASE;
  if (v === undefined || v === null || String(v).trim() === "") {
    return "";
  }
  return String(v).replace(/\/$/, "");
}

export const GATEWAY_BASE = resolveGatewayBase();
