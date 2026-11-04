/**
 * Separate operator/admin login screen (Superadmin token), without user portal.
 */
import { GATEWAY_BASE } from "../../infrastructure/gateway";

export function superadminLoginPageHtml(): string {
  return `
<div class="lp-auth-page">
  <div class="lp-auth-card lp-auth-card--admin">
    <div class="lp-auth-promo lp-auth-promo--admin">
      <h2 class="lp-auth-promo-title">Administration</h2>
      <p class="lp-auth-promo-text">Token from <code>SUPERADMIN_TOKEN</code> (compose / .env). Do not share it with third parties.</p>
      <a class="lp-auth-promo-btn lp-auth-promo-btn--ghost" href="#/login">User portal sign-in</a>
    </div>
    <div class="lp-auth-form-side">
      <h1 class="lp-auth-form-title">Superadmin</h1>
      <p class="lp-auth-form-sub">Platform operator panel</p>
      <div class="lp-auth-field">
        <span class="lp-auth-field-icon" aria-hidden="true">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
        </span>
        <input class="lp-auth-input" type="password" id="superadmin-login-token" autocomplete="off" placeholder="Superadmin token" />
      </div>
      <p class="lp-auth-error" id="superadmin-login-error" style="display:none"></p>
      <button type="button" class="lp-auth-submit" id="superadmin-login-submit">Sign in</button>
      <p class="lp-auth-hint lp-muted" style="margin-top:14px;font-size:12px;">Gateway: <code>${GATEWAY_BASE || "(same origin)"}</code></p>
    </div>
  </div>
</div>`;
}
