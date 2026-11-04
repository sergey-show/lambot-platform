/**
 * Domain: portal user session and tokens (auth context boundary).
 */
export const LS_USER_JWT = "lambot_user_jwt";
export const LS_SUPERADMIN_TOKEN = "lambot_superadmin_token";

export function parseJwtPayload(token: string): { roles?: string[]; email?: string } | null {
  try {
    const part = token.split(".")[1];
    if (!part) return null;
    const json = atob(part.replace(/-/g, "+").replace(/_/g, "/"));
    return JSON.parse(json) as { roles?: string[]; email?: string };
  } catch {
    return null;
  }
}

export function isPortalUserSession(): boolean {
  const j = localStorage.getItem(LS_USER_JWT)?.trim();
  if (!j) return false;
  const pl = parseJwtPayload(j);
  const roles = pl?.roles;
  if (Array.isArray(roles) && roles.includes("Superadmin")) return false;
  return true;
}

export function getSuperadminTokenForUi(stateFallback: string): string {
  return localStorage.getItem(LS_SUPERADMIN_TOKEN)?.trim() || stateFallback;
}
