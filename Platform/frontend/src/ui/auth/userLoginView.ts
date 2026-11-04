/**
 * View: user login (split-card, reference style).
 */
import { GATEWAY_BASE } from "../../infrastructure/gateway";

export function userLoginPageHtml(): string {
  return `
<div class="lp-auth-page">
  <div class="lp-auth-card">
    <div class="lp-auth-promo">
      <h2 class="lp-auth-promo-title">Hello!</h2>
      <p class="lp-auth-promo-text">No account? Ask an administrator to create a user in Superadmin.</p>
    </div>
    <div class="lp-auth-form-side">
      <h1 class="lp-auth-form-title">Sign in</h1>
      <p class="lp-auth-form-sub">User portal Lambot</p>
      <div class="lp-auth-field">
        <span class="lp-auth-field-icon" aria-hidden="true">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
        </span>
        <input class="lp-auth-input" type="email" id="login-email" autocomplete="username" placeholder="Email" />
      </div>
      <div class="lp-auth-field">
        <span class="lp-auth-field-icon" aria-hidden="true">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
        </span>
        <input class="lp-auth-input" type="password" id="login-password" autocomplete="current-password" placeholder="Password" />
      </div>
      <p class="lp-auth-error" id="login-error" style="display:none"></p>
      <button type="button" class="lp-auth-submit" id="login-submit">Sign in</button>
      <p class="lp-auth-hint lp-muted" style="margin-top:14px;font-size:12px;">Gateway: <code>${GATEWAY_BASE || "(same origin)"}</code></p>
    </div>
  </div>
</div>`;
}
