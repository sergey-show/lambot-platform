import "./styles.css";
import {
  getEditorContent,
  mountAnsibleSwarmEditors,
  setEditorContent,
  type AnsibleSwarmEditorPair,
} from "./ansibleSwarmEditor";
import {
  getDocsMarkdownContent,
  mountDocsMarkdownEditor,
  setDocsMarkdownContent,
} from "./docsMarkdownEditor";
import DOMPurify from "dompurify";
import { marked } from "marked";
import { escapeHtml } from "./shared/html";
import { GATEWAY_BASE } from "./infrastructure/gateway";
import {
  LS_USER_JWT,
  LS_SUPERADMIN_TOKEN,
  isPortalUserSession,
  getSuperadminTokenForUi as superadminTokenFromStorage,
} from "./domains/auth/session";
import { bindPortalNav, portalViewFromRoute, syncPortalView } from "./domains/portal/navigation";
import { userLoginPageHtml } from "./ui/auth/userLoginView";
import { superadminLoginPageHtml } from "./ui/auth/superadminLoginView";

type ProjectRow = {
  id: string;
  name: string;
  description?: string;
  tenant_id: string;
  status: string;
  suspended?: boolean;
  deleted_at?: string | null;
  created_at: string;
};

let lastAdminProjectsSnapshot: ProjectRow[] = [];
let superadminPhasePollTimer: number | null = null;

type AiTeamLiveRun = {
  run_id: string;
  project_id: string;
  project_name?: string;
  status: string;
  phase?: string | null;
  iteration?: number;
  batch_status?: string;
  updated_at?: string | null;
  created_at?: string | null;
  error?: string | null;
};

marked.setOptions({ gfm: true, breaks: true });

function renderMarkdownHtml(input: string): string {
  const src = String(input || "");
  const parsed = marked.parse(src, { async: false }) as string;
  return DOMPurify.sanitize(parsed, { USE_PROFILES: { html: true } });
}

function projectAiChatStorageKey(projectId: string): string {
  return `lambot_project_ai_chat_v1_${projectId}`;
}

const state = {
  /** Опционально: только для локальных скриптов; в UI по умолчанию пусто. */
  userToken: "",
  superadminToken: "",
};

function getPortalToken(): string {
  const fromLs = localStorage.getItem(LS_USER_JWT)?.trim();
  if (fromLs) return fromLs;
  return state.userToken;
}

function getSuperadminTokenForUi(): string {
  return superadminTokenFromStorage(state.superadminToken);
}

function navVariant(): "portal" | "full" {
  return isPortalUserSession() ? "portal" : "full";
}

/** Chart.js from CDN (see index.html) */
type ChartLike = { destroy: () => void };
let chartRegistry: ChartLike[] = [];

function destroyCharts(): void {
  chartRegistry.forEach((c) => {
    try {
      c.destroy();
    } catch {
      /* noop */
    }
  });
  chartRegistry = [];
}

function route(): string {
  const h = location.hash.replace("#", "").trim();
  if (!h || h === "/") return "/login";
  return h.startsWith("/") ? h : "/login";
}

function authHeader(token: string): Record<string, string> {
  return { Authorization: `Bearer ${token}` };
}

const appEl = () => document.getElementById("app");

function mount(html: string): void {
  const app = appEl();
  if (!app) return;
  destroyCharts();
  (window as unknown as { __lpPortalMounted?: boolean }).__lpPortalMounted = false;
  app.innerHTML = html;
}

function iconDashboard(): string {
  return `<svg class="lp-nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/></svg>`;
}
function iconPortal(): string {
  return `<svg class="lp-nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></svg>`;
}
function iconShield(): string {
  return `<svg class="lp-nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>`;
}

function shell(params: {
  active: "dashboard" | "portal" | "superadmin" | "login";
  breadcrumb: string;
  title: string;
  children: string;
  /** full = оператор (нет JWT портала); portal = только user portal + login */
  nav?: "portal" | "full";
}): string {
  const { active, breadcrumb, title, children } = params;
  const nav = params.nav ?? navVariant();
  const dash = active === "dashboard" ? " lp-active" : "";
  const port = active === "portal" ? " lp-active" : "";
  const adm = active === "superadmin" ? " lp-active" : "";
  const log = active === "login" ? " lp-active" : "";
  const brandHref = nav === "portal" ? "#/portal/home" : "#/login";
  const adminNav =
    nav === "portal"
      ? ""
      : `
      <a href="#/dashboard" class="${dash}">${iconDashboard()} Dashboard</a>
      <a href="#/admin" class="${adm}">${iconShield()} Superadmin</a>
      `;
  return `
<div class="lp-app">
  <aside class="lp-sidebar">
    <a class="lp-brand" href="${brandHref}">
      <span class="lp-brand-mark">L</span>
      <span class="lp-brand-text">Lambot</span>
    </a>
    <div class="lp-nav-label">Platform</div>
    <nav class="lp-nav">
      ${adminNav}
      <a href="#/portal/home" class="${port}">${iconPortal()} User portal</a>
      <a href="#/login" class="${log}">${iconPortal()} Login</a>
    </nav>
    <div class="lp-nav-label">Services</div>
    <nav class="lp-nav">
      <span class="lp-muted" style="padding:0.5rem 0.85rem;font-size:0.8rem;">Gateway · Kafka · Postgres</span>
    </nav>
    <div class="lp-sidebar-foot">Multi-agent control plane</div>
  </aside>
  <div class="lp-main">
    <header class="lp-topbar">
      <div class="lp-search">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/></svg>
        <input type="search" placeholder="Search projects, tenants, targets…" autocomplete="off" />
      </div>
      <div class="lp-topbar-actions">
        <button type="button" class="lp-icon-btn" title="Notifications" aria-label="Notifications">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>
        </button>
        <button type="button" class="lp-icon-btn" title="Messages" aria-label="Messages">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
        </button>
        <div class="lp-avatar" title="Operator"></div>
      </div>
    </header>
    <div class="lp-content">
      <div class="lp-breadcrumb">${breadcrumb}</div>
      <div class="lp-page-title">
        <h1>${title}</h1>
        <div class="lp-action-pills">
          <button type="button" class="lp-pill" style="background:#ede9fe;" title="Add">+</button>
          <button type="button" class="lp-pill" style="background:#cffafe;" title="Export">↗</button>
          <button type="button" class="lp-pill" style="background:#fce7f3;" title="Settings">⚙</button>
        </div>
      </div>
      ${children}
    </div>
  </div>
</div>`;
}

/** Global from chart.js UMD (index.html) */
type ChartConstructor = new (
  ctx: HTMLCanvasElement,
  config: Record<string, unknown>,
) => ChartLike;

function getChart(): ChartConstructor | null {
  const w = window as unknown as { Chart?: ChartConstructor };
  return w.Chart ?? null;
}

function renderDashboard(): void {
  mount(
    shell({
      active: "dashboard",
      breadcrumb: `Home <span>/</span> Dashboard`,
      title: "Admin",
      children: `
      <div class="lp-banner">
        <span>●</span>
        <span><strong>Platform status:</strong> services polled on load — use Superadmin for filters &amp; raw metrics.</span>
      </div>
      <div class="lp-panel">
        <div class="lp-row">
          <div class="lp-field" style="flex:1">
            <label>Superadmin token</label>
            <input class="lp-input" id="dashboard-sa-token" type="password" autocomplete="off" placeholder="Вставьте токен (как в compose / .env)" value="${escapeHtml(getSuperadminTokenForUi())}" style="max-width:480px" />
          </div>
          <button type="button" class="lp-btn" id="dashboard-sa-apply">Сохранить и обновить</button>
        </div>
        <p class="lp-muted">Сохраняется в localStorage этого браузера. Для демо используйте тот же токен, что и <code>SUPERADMIN_TOKEN</code> у gateway.</p>
      </div>
      <section class="lp-hero">
        <div>
          <h2>Welcome back, operator</h2>
          <p>Orchestrate agents, infra, and delivery from one control plane.</p>
          <div class="lp-hero-avatars" aria-hidden="true">
            <div class="lp-mini-av"></div>
            <div class="lp-mini-av" style="background:linear-gradient(135deg,#06b6d4,#7c3aed)"></div>
            <div class="lp-mini-av" style="background:linear-gradient(135deg,#22c55e,#ec4899)"></div>
          </div>
        </div>
        <div class="lp-hero-stats">
          <div class="lp-hero-stat">
            <b id="hero-total">—</b>
            <span>Projects</span>
          </div>
          <div class="lp-hero-stat">
            <b id="hero-healthy">—</b>
            <span>Healthy services</span>
          </div>
        </div>
      </section>
      <div class="lp-stat-grid">
        <article class="lp-stat-card">
          <div>
            <h3>Total projects</h3>
            <div class="lp-big" id="kpi-total">—</div>
          </div>
          <div class="lp-donut-wrap"><canvas id="donut-total" width="72" height="72"></canvas></div>
        </article>
        <article class="lp-stat-card">
          <div>
            <h3>Plan ready</h3>
            <div class="lp-big" id="kpi-plan">—</div>
          </div>
          <div class="lp-donut-wrap"><canvas id="donut-plan" width="72" height="72"></canvas></div>
        </article>
        <article class="lp-stat-card">
          <div>
            <h3>Infra in progress</h3>
            <div class="lp-big" id="kpi-infra">—</div>
          </div>
          <div class="lp-donut-wrap"><canvas id="donut-infra" width="72" height="72"></canvas></div>
        </article>
        <article class="lp-stat-card">
          <div>
            <h3>Deployed</h3>
            <div class="lp-big" id="kpi-deployed">—</div>
          </div>
          <div class="lp-donut-wrap"><canvas id="donut-deployed" width="72" height="72"></canvas></div>
        </article>
      </div>
      <div class="lp-chart-card">
        <h2>Throughput &amp; activity</h2>
        <p class="lp-muted" style="margin:-0.5rem 0 1rem;">Illustrative weekly series — wire Prometheus for live series.</p>
        <canvas id="lp-main-chart" height="120"></canvas>
      </div>
      `,
    }),
  );

  const applyDash = () => {
    const v = (document.getElementById("dashboard-sa-token") as HTMLInputElement).value.trim();
    if (v) localStorage.setItem(LS_SUPERADMIN_TOKEN, v);
    else localStorage.removeItem(LS_SUPERADMIN_TOKEN);
    void loadDashboardData(v);
  };
  document.getElementById("dashboard-sa-apply")?.addEventListener("click", applyDash);
  void loadDashboardData(getSuperadminTokenForUi());
}

async function loadDashboardData(token: string): Promise<void> {
  const setText = (id: string, v: string | number) => {
    const n = document.getElementById(id);
    if (n) n.textContent = String(v);
  };

  try {
    const [overviewResp, healthResp] = await Promise.all([
      fetch(`${GATEWAY_BASE}/api/v1/admin/overview`, { headers: authHeader(token) }),
      fetch(`${GATEWAY_BASE}/api/v1/admin/services/health`, { headers: authHeader(token) }),
    ]);
    const overview = await overviewResp.json();
    const healthPayload = await healthResp.json();
    const byStatus = (overview.projects_by_status ?? {}) as Record<string, number>;
    const total = Number(overview.projects_total ?? 0);
    const plan = Number(byStatus.PLAN_READY ?? 0);
    const infra = Number(byStatus.INFRA_IN_PROGRESS ?? 0);
    const deployed = Number(byStatus.DEPLOYED ?? 0);

    setText("kpi-total", total);
    setText("kpi-plan", plan);
    setText("kpi-infra", infra);
    setText("kpi-deployed", deployed);
    setText("hero-total", total);

    const services = (healthPayload.services ?? {}) as Record<string, string>;
    const okCount = Object.values(services).filter((s) => s === "ok").length;
    const svcTotal = Object.keys(services).length || 1;
    setText("hero-healthy", `${okCount}/${svcTotal}`);

    const Chart = getChart();
    if (Chart) {
      const pct = (n: number) => (total > 0 ? Math.round((n / total) * 100) : 0);
      const ring = (id: string, value: number, color: string) => {
        const el = document.getElementById(id) as HTMLCanvasElement | null;
        if (!el) return;
        const rest = Math.max(0, 100 - value);
        const c = new Chart(el, {
          type: "doughnut",
          data: {
            datasets: [
              {
                data: [value, rest],
                backgroundColor: [color, "#ede9fe"],
                borderWidth: 0,
              },
            ],
          },
          options: {
            responsive: true,
            cutout: "72%",
            plugins: { legend: { display: false }, tooltip: { enabled: false } },
          },
        });
        chartRegistry.push(c);
      };
      ring("donut-total", Math.min(100, total > 0 ? 100 : 0), "#7c3aed");
      ring("donut-plan", pct(plan), "#06b6d4");
      ring("donut-infra", pct(infra), "#f59e0b");
      ring("donut-deployed", pct(deployed), "#22c55e");

      const mixEl = document.getElementById("lp-main-chart") as HTMLCanvasElement | null;
      if (mixEl) {
        const labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
        const bars = labels.map((_, i) => 12 + ((i * 7) % 18) + (total % 5));
        const line = labels.map((_, i) => 20 + Math.sin(i * 0.8) * 12 + (okCount % 7));
        const mixed = new Chart(mixEl, {
          type: "bar",
          data: {
            labels,
            datasets: [
              {
                label: "Tasks completed",
                data: bars,
                backgroundColor: labels.map((_, i) =>
                  i % 2 === 0 ? "rgba(124,58,237,0.78)" : "rgba(34,197,94,0.62)",
                ),
                borderRadius: 6,
                order: 2,
              },
              {
                type: "line",
                label: "Rolling load",
                data: line,
                borderColor: "#0ea5e9",
                backgroundColor: "rgba(14,165,233,0.12)",
                fill: true,
                tension: 0.35,
                yAxisID: "y",
                order: 1,
              },
            ],
          },
          options: {
            responsive: true,
            interaction: { mode: "index", intersect: false },
            plugins: { legend: { position: "bottom" } },
            scales: {
              y: { beginAtZero: true, grid: { color: "rgba(148,163,184,0.2)" } },
              x: { grid: { display: false } },
            },
          },
        });
        chartRegistry.push(mixed);
      }
    }
  } catch (e) {
    setText("hero-total", "!");
    setText("hero-healthy", "—");
    const banner = document.querySelector(".lp-banner");
    if (banner) banner.innerHTML = `<span>⚠</span> <strong>Could not load dashboard.</strong> Check gateway at ${GATEWAY_BASE} and superadmin token.`;
  }
}

function renderLogin(): void {
  mount(userLoginPageHtml());
  document.getElementById("login-submit")?.addEventListener("click", async () => {
    const email = (document.getElementById("login-email") as HTMLInputElement).value.trim();
    const password = (document.getElementById("login-password") as HTMLInputElement).value;
    const err = document.getElementById("login-error");
    if (err) {
      err.style.display = "none";
      err.textContent = "";
    }
    const setErr = (msg: string) => {
      if (err) {
        err.style.display = "block";
        err.textContent = msg;
      }
    };
    const resp = await fetch(`${GATEWAY_BASE}/api/v1/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    const data = await resp.json();
    if (!resp.ok) {
      if (err) {
        err.style.display = "block";
        err.textContent = (data as { detail?: string }).detail || JSON.stringify(data);
      }
      return;
    }
    const tok = (data as { access_token?: string }).access_token;
    if (tok) localStorage.setItem(LS_USER_JWT, tok);
    location.hash = "#/portal/home";
    renderRoute();
  });
}

function renderSuperadminLogin(): void {
  mount(superadminLoginPageHtml());
  const errEl = document.getElementById("superadmin-login-error");
  const setErr = (msg: string) => {
    if (errEl) {
      errEl.style.display = msg ? "block" : "none";
      errEl.textContent = msg;
    }
  };
  document.getElementById("superadmin-login-submit")?.addEventListener("click", async () => {
    const tok = (document.getElementById("superadmin-login-token") as HTMLInputElement).value.trim();
    setErr("");
    if (!tok) {
      setErr("Введите Superadmin token");
      return;
    }
    try {
      const resp = await fetch(`${GATEWAY_BASE}/api/v1/admin/overview`, {
        headers: { Authorization: `Bearer ${tok}` },
      });
      if (!resp.ok) {
        const raw = await resp.text();
        let detail = raw;
        try {
          detail = String((JSON.parse(raw) as { detail?: string }).detail || raw);
        } catch {
          /* raw text */
        }
        setErr(detail || `Ошибка ${resp.status}`);
        return;
      }
    } catch (e) {
      setErr(String(e));
      return;
    }
    localStorage.setItem(LS_SUPERADMIN_TOKEN, tok);
    state.superadminToken = tok;
    location.hash = "#/dashboard";
    renderRoute();
  });
}

function renderPortal(): void {
  mount(
    shell({
      active: "portal",
      breadcrumb: `Home <span>/</span> User portal`,
      title: "User portal",
      children: `
      <div class="lp-portal-app">
      <aside class="lp-portal-sidebar" aria-label="Разделы проекта">
        <div class="lp-portal-side-title">Разделы</div>
        <nav class="lp-portal-nav">
          <a href="#/portal/home" data-portal-view="home">Главная</a>
          <a href="#/portal/team" data-portal-view="team">ИИ Команда</a>
          <a href="#/portal/docs" data-portal-view="docs">Документация</a>
          <a href="#/portal/tracker" data-portal-view="tracker">Задачи агентов</a>
          <a href="#/portal/servers" data-portal-view="servers">Серверы</a>
          <a href="#/portal/k8s-install" data-portal-view="k8s-install">K8s: установка</a>
          <a href="#/portal/k8s" data-portal-view="k8s">K8s: управление</a>
          <a href="#/portal/infra" data-portal-view="infra">Инфраструктура</a>
          <a href="#/portal/service" data-portal-view="service">Служебное</a>
        </nav>
      </aside>
      <div class="lp-portal-outlet" id="lp-portal-outlet">
      <div id="view-home" class="lp-portal-view lp-portal-view--active">
      <div class="lp-banner"><span>●</span><span><strong>Демо:</strong> войдите через <a href="#/login">Login</a> (JWT). Без входа можно использовать legacy token ниже.</span></div>
      <div class="lp-panel">
        <div class="lp-row">
          <div class="lp-field"><label>Сессия</label>
            <div id="portal-session" class="lp-muted">…</div>
          </div>
          <button type="button" class="lp-btn lp-btn-ghost" id="portal-logout">Выйти (очистить JWT)</button>
        </div>
        <div class="lp-row">
          <div class="lp-field"><label>Legacy bearer (dev)</label>
            <input class="lp-input" id="user-token" value="${state.userToken}" style="max-width:420px;" />
          </div>
          <button type="button" class="lp-btn lp-btn-ghost" id="load-catalog">Catalog</button>
          <button type="button" class="lp-btn lp-btn-ghost" id="portal-refresh-projects">Обновить проекты</button>
        </div>
      </div>
      <div class="lp-panel">
        <h2>Проект</h2>
        <div class="lp-row">
          <div class="lp-field"><label>Выбрать проект</label>
            <select class="lp-select" id="project-select" style="min-width:280px;"><option value="">— создайте новый —</option></select>
          </div>
        </div>
        <div class="lp-row">
          <div class="lp-field"><label>Имя (новый)</label><input class="lp-input" id="project-name" value="Мой проект" /></div>
          <div class="lp-field"><label>Tenant (только legacy token)</label><input class="lp-input" id="tenant-id" value="11111111-1111-1111-1111-111111111111" style="min-width:280px;" /></div>
        </div>
        <div class="lp-field"><label>Описание</label><textarea class="lp-textarea" id="project-desc" rows="2">Демо-проект</textarea></div>
        <div class="lp-row">
          <button type="button" class="lp-btn" id="create-project">Создать проект</button>
        </div>
        <div class="lp-row" style="margin-top:12px;align-items:center;gap:12px;">
          <div><strong>Статус:</strong> <span id="project-status-label" class="lp-badge">—</span></div>
          <button type="button" class="lp-btn lp-btn-ghost" id="refresh-status">Обновить статус</button>
        </div>
      </div>
      <div class="lp-panel lp-project-ai-chat">
        <h2>ИИ-чат по проекту</h2>
        <p class="lp-muted">Один чат с мультиагентной системой платформы: можно описать стартап или продукт — агент запустит планирование (MVP), подскажет по инфраструктуре, репозиториям и статусам; при необходимости подтянет логи и метрики. Нужен LLM в Superadmin / OPENAI_API_KEY.</p>
        <p class="lp-muted" id="project-ai-chat-gate" style="font-size:13px;margin-bottom:8px;"></p>
        <div id="project-ai-chat-thread" class="lp-ai-chat-thread" role="log" aria-live="polite"></div>
        <div class="lp-row" style="flex-wrap:wrap;align-items:flex-end;gap:8px;margin-top:12px;">
          <div class="lp-field" style="flex:1;min-width:240px;">
            <label for="project-ai-chat-input">Сообщение</label>
            <textarea class="lp-textarea" id="project-ai-chat-input" rows="3" placeholder="Например: хочу сервис доставки еды для малого города — с чего начать? Или: какой статус проекта и что в последних логах?" style="width:100%;max-width:100%;min-height:88px;"></textarea>
          </div>
          <div class="lp-row" style="gap:8px;flex-wrap:wrap;">
            <button type="button" class="lp-btn" id="project-ai-chat-send">Отправить</button>
            <button type="button" class="lp-btn lp-btn-ghost" id="project-ai-chat-clear">Очистить чат</button>
          </div>
          <p class="lp-muted" style="font-size:12px;margin-top:6px;">Оркестрация запускается автоматически из этого чата в рамках текущего run.</p>
        </div>
        <p class="lp-muted" id="project-ai-chat-status" style="font-size:12px;margin-top:8px;"></p>
      </div>
      </div>
      <div id="view-team" class="lp-portal-view">
      <div class="lp-panel">
        <div class="lp-field">
          <label for="orchestration-user-message">Запрос для оркестрации</label>
          <textarea class="lp-textarea" id="orchestration-user-message" rows="3" placeholder="Сформулируйте задачу (можно скопировать из чата по проекту)"></textarea>
        </div>
        <div class="lp-row" style="margin-bottom:12px;">
          <button type="button" class="lp-btn" id="project-orchestrate-btn" title="PM → Архитектор → DevOps → Backend → Frontend">Запустить оркестрацию</button>
        </div>
      </div>
      <div class="lp-panel" id="orchestration-panel">
        <h2>ИИ Команда</h2>
        <p class="lp-muted">
          Состояние хранится в Postgres: маршрутизация → PM → архитектура (видна ниже) → планы DevOps/Backend/Frontend → реализация → деплой → QA → итоговый отчёт PM. Промпты ролей — в Superadmin. Можно запускать из чата автоматически или вручную кнопкой выше.
        </p>
        <p class="lp-muted" id="orchestration-status-line" style="font-size:13px;margin-bottom:8px;"></p>
        <details id="orchestration-live-log-wrap" style="margin:0 0 10px;">
          <summary class="lp-muted" style="cursor:pointer;font-size:13px;">Realtime лог действий агента</summary>
          <pre id="orchestration-live-log" class="lp-pre" style="max-height:260px;overflow:auto;margin-top:8px;font-size:11px;white-space:pre-wrap;word-break:break-word;"></pre>
        </details>
        <details id="orchestration-state-wrap" style="margin:0 0 10px;">
          <summary class="lp-muted" style="cursor:pointer;font-size:13px;">DAG state (live JSON)</summary>
          <pre id="orchestration-state-json" class="lp-pre" style="max-height:340px;overflow:auto;margin-top:8px;font-size:11px;white-space:pre-wrap;word-break:break-word;"></pre>
        </details>
        <div id="orchestration-graph-wrap"></div>
        <div id="orchestration-arch-wrap"></div>
        <div id="orchestration-exec-wrap"></div>
        <div id="orchestration-steps" class="lp-orch-grid"></div>
        <div id="orchestration-final-wrap" style="margin-top:12px;"></div>
      </div>
      <div class="lp-panel" id="browser-logs-panel">
        <h2>Browser tool: действия и скриншоты</h2>
        <p class="lp-muted" style="font-size:12px;">
          Логи браузерных действий агентов (Playwright) по текущему проекту и run. Для скриншотов нажмите «Открыть».
        </p>
        <p class="lp-muted" id="browser-logs-status-line" style="font-size:12px;"></p>
        <div id="browser-logs-wrap" class="lp-orch-grid"></div>
      </div>
      <div id="orchestration-node-modal" class="lp-modal-backdrop" hidden>
        <div class="lp-modal" role="dialog" aria-modal="true" aria-labelledby="orch-node-modal-title">
          <div class="lp-modal-head">
            <h3 id="orch-node-modal-title" style="margin:0;">Детали шага</h3>
            <button type="button" class="lp-btn lp-btn-ghost" id="orch-node-modal-close">Закрыть</button>
          </div>
          <div id="orch-node-modal-body"></div>
        </div>
      </div>
      </div>
      <div id="view-docs" class="lp-portal-view">
      <div class="lp-panel" id="docs-panel">
        <h2>Документация проекта (Wiki + RAG)</h2>
        <p class="lp-muted">
          Здесь хранится актуальная архитектура и инструкции для пользователя/администратора. Агенты используют эти документы как контекст при решении следующих задач.
        </p>
        <div class="lp-row" style="flex-wrap:wrap;gap:10px;align-items:flex-end;margin-bottom:12px;">
          <div class="lp-field" style="min-width:190px;">
            <label>Фильтр типа</label>
            <select class="lp-select" id="docs-filter-type">
              <option value="">Все</option>
              <option value="architecture">architecture</option>
              <option value="user_guide">user_guide</option>
              <option value="admin_guide">admin_guide</option>
              <option value="runbook">runbook</option>
              <option value="adr">adr</option>
              <option value="api_contract">api_contract</option>
              <option value="other">other</option>
            </select>
          </div>
          <button type="button" class="lp-btn lp-btn-ghost" id="docs-refresh-btn">Обновить список</button>
        </div>
        <p class="lp-muted" id="docs-status-line" style="font-size:12px;margin-bottom:8px;"></p>
        <div id="docs-items-list" class="lp-orch-grid"></div>
      </div>
      <div class="lp-panel">
        <h3 style="margin-top:0;">Редактор документа</h3>
        <input type="hidden" id="docs-edit-id" value="" />
        <div class="lp-row" style="flex-wrap:wrap;gap:10px;">
          <div class="lp-field" style="min-width:240px;flex:1;">
            <label>Заголовок</label>
            <input class="lp-input" id="docs-edit-title" placeholder="Например, Architecture Overview" />
          </div>
          <div class="lp-field" style="min-width:180px;">
            <label>Тип</label>
            <select class="lp-select" id="docs-edit-type">
              <option value="architecture">architecture</option>
              <option value="user_guide">user_guide</option>
              <option value="admin_guide">admin_guide</option>
              <option value="runbook">runbook</option>
              <option value="adr">adr</option>
              <option value="api_contract">api_contract</option>
              <option value="other">other</option>
            </select>
          </div>
          <div class="lp-field" style="min-width:220px;">
            <label>Slug (опционально)</label>
            <input class="lp-input" id="docs-edit-slug" placeholder="architecture-overview" />
          </div>
        </div>
        <div class="lp-field">
          <label>Содержимое (Markdown)</label>
          <div class="lp-row" style="gap:10px;align-items:stretch;">
            <div style="flex:1;min-width:320px;">
              <div id="docs-md-editor" class="ansible-ide-editor" aria-label="markdown editor"></div>
            </div>
            <div style="flex:1;min-width:320px;">
              <div class="lp-muted" style="font-size:12px;margin-bottom:6px;">Preview</div>
              <div id="docs-md-preview" class="lp-markdown lp-pre" style="max-height:420px;overflow:auto;"></div>
            </div>
          </div>
        </div>
        <div class="lp-field">
          <label>Комментарий версии</label>
          <input class="lp-input" id="docs-edit-change-summary" placeholder="Что изменено" />
        </div>
        <div class="lp-row" style="gap:8px;">
          <button type="button" class="lp-btn" id="docs-save-btn">Сохранить версию</button>
          <button type="button" class="lp-btn lp-btn-ghost" id="docs-new-btn">Новый документ</button>
        </div>
        <h4 style="margin:14px 0 8px;">История версий</h4>
        <div id="docs-versions-wrap" class="lp-orch-grid"></div>
      </div>
      <div class="lp-panel">
        <h3 style="margin-top:0;">Поиск по документации (RAG)</h3>
        <div class="lp-row" style="gap:8px;align-items:flex-end;">
          <div class="lp-field" style="flex:1;">
            <label>Запрос</label>
            <input class="lp-input" id="docs-search-query" placeholder="например: как устроен деплой и rollback" />
          </div>
          <button type="button" class="lp-btn lp-btn-ghost" id="docs-search-btn">Поиск</button>
        </div>
        <div id="docs-search-results" class="lp-orch-grid"></div>
      </div>
      </div>
      <div id="view-tracker" class="lp-portal-view">
      <div class="lp-panel" id="tracker-panel">
        <h2>Задачи агентов</h2>
        <p class="lp-muted">
          Создайте задачу, назначьте роль агента (ключ вида <code>agent:project:…:backend</code>) и нажмите «В очередь» — исполнение пойдёт через Kafka в agent-worker (при <code>AGENT_WORKER_USE_LLM=1</code> — вызов LLM). Пока статус не <code>queued</code> / <code>in_progress</code> / <code>done</code> / <code>cancelled</code>, можно редактировать текст и роль. «Закрыть» — отмена (статус <code>cancelled</code>).
        </p>
        <div class="lp-row" style="flex-wrap:wrap;gap:10px;align-items:flex-end;margin-bottom:12px;">
          <div class="lp-field" style="min-width:180px;">
            <label>Тип</label>
            <select class="lp-select" id="tracker-new-kind">
              <option value="task">Задача</option>
              <option value="epic">Эпик</option>
            </select>
          </div>
          <div class="lp-field" style="flex:1;min-width:200px;">
            <label>Заголовок</label>
            <input class="lp-input" id="tracker-new-title" placeholder="Кратко" style="width:100%;" />
          </div>
          <div class="lp-field" style="min-width:160px;">
            <label>Роль агента</label>
            <select class="lp-select" id="tracker-new-role">
              <option value="">—</option>
              <option value="pm">PM</option>
              <option value="architect">Архитектор</option>
              <option value="devops">DevOps</option>
              <option value="backend">Backend</option>
              <option value="frontend">Frontend</option>
              <option value="qa">QA</option>
            </select>
          </div>
        </div>
        <div class="lp-field">
          <label>Описание</label>
          <textarea class="lp-textarea" id="tracker-new-desc" rows="2" placeholder="Что сделать"></textarea>
        </div>
        <div class="lp-row" style="gap:8px;margin-bottom:12px;">
          <button type="button" class="lp-btn" id="tracker-create-btn">Создать</button>
          <button type="button" class="lp-btn lp-btn-ghost" id="tracker-refresh-btn">Обновить список</button>
        </div>
        <p class="lp-muted" id="tracker-status-line" style="font-size:12px;margin-bottom:8px;"></p>
        <div id="tracker-edit-wrap" style="display:none;margin-bottom:14px;padding:12px;border:1px solid rgba(148,163,184,0.35);border-radius:10px;background:rgba(248,250,252,0.6);">
          <h3 style="font-size:15px;margin:0 0 10px;">Редактирование карточки</h3>
          <input type="hidden" id="tracker-edit-id" value="" />
          <input type="hidden" id="tracker-edit-kind" value="task" />
          <div class="lp-field">
            <label for="tracker-edit-title">Заголовок</label>
            <input class="lp-input" id="tracker-edit-title" style="width:100%;max-width:520px;" />
          </div>
          <div class="lp-field">
            <label for="tracker-edit-desc">Описание</label>
            <textarea class="lp-textarea" id="tracker-edit-desc" rows="3" style="width:100%;max-width:520px;"></textarea>
          </div>
          <div class="lp-field" id="tracker-edit-role-row">
            <label for="tracker-edit-role">Роль агента (для задачи)</label>
            <select class="lp-select" id="tracker-edit-role" style="max-width:280px;">
              <option value="">—</option>
              <option value="pm">PM</option>
              <option value="architect">Архитектор</option>
              <option value="devops">DevOps</option>
              <option value="backend">Backend</option>
              <option value="frontend">Frontend</option>
              <option value="qa">QA</option>
            </select>
          </div>
          <div class="lp-row" style="gap:8px;margin-top:8px;">
            <button type="button" class="lp-btn" id="tracker-edit-save">Сохранить</button>
            <button type="button" class="lp-btn lp-btn-ghost" id="tracker-edit-cancel">Отмена</button>
          </div>
        </div>
        <div id="tracker-items-list" class="lp-orch-grid"></div>
      </div>
      <div id="tracker-task-modal" class="lp-modal-backdrop" hidden>
        <div class="lp-modal" role="dialog" aria-modal="true" aria-labelledby="tracker-task-modal-title">
          <div class="lp-modal-head">
            <h3 id="tracker-task-modal-title" style="margin:0;">Результат задачи</h3>
            <button type="button" class="lp-btn lp-btn-ghost" id="tracker-task-modal-close">Закрыть</button>
          </div>
          <div id="tracker-task-modal-body"></div>
        </div>
      </div>
      </div>
      <div id="view-servers" class="lp-portal-view">
      <div class="lp-panel">
        <h2>Сервер (SSH / Kubernetes)</h2>
        <p class="lp-muted">Учётные данные шифруются. <strong>SSH</strong> — проверка, затем сохранение: агент ставится автоматически (нужен <code>sudo</code> без пароля). <strong>Kubernetes</strong> — вставьте kubeconfig (доступ к API кластера), агент на ноды не ставится.</p>
        <div class="lp-field"><label>Тип подключения</label>
          <select class="lp-select" id="srv-kind" style="max-width:280px;">
            <option value="ssh">SSH (сервер и агент)</option>
            <option value="kubernetes">Kubernetes (kubeconfig)</option>
          </select>
        </div>
        <div id="srv-ssh-block">
        <div class="lp-row">
          <div class="lp-field"><label>Host</label><input class="lp-input" id="srv-host" placeholder="203.0.113.10" style="max-width:220px;" /></div>
          <div class="lp-field"><label>Port</label><input class="lp-input" id="srv-port" type="number" value="22" style="max-width:80px;" /></div>
          <div class="lp-field"><label>SSH user</label><input class="lp-input" id="srv-user" value="ubuntu" style="max-width:140px;" /></div>
        </div>
        <div class="lp-field"><label>Auth</label>
          <select class="lp-select" id="srv-auth" style="max-width:200px;"><option value="key">Private key</option><option value="password">Password</option></select>
        </div>
        <div class="lp-field"><label>Secret</label><textarea class="lp-textarea" id="srv-secret" rows="4" placeholder="PEM или пароль"></textarea></div>
        </div>
        <div id="srv-k8s-block" style="display:none;">
          <div class="lp-field"><label>Имя кластера (для списка)</label><input class="lp-input" id="srv-k8s-name" placeholder="Мой кластер" style="max-width:320px;" /></div>
          <div class="lp-field"><label>Контекст kubectl (необязательно)</label><input class="lp-input" id="srv-k8s-context" placeholder="Пусто = контекст по умолчанию" style="max-width:320px;" /></div>
          <div class="lp-field"><label>kubeconfig</label><textarea class="lp-textarea" id="srv-k8s-config" rows="10" placeholder="Вставьте содержимое файла ~/.kube/config"></textarea></div>
        </div>
        <div class="lp-row" style="flex-wrap:wrap;gap:8px;">
          <button type="button" class="lp-btn lp-btn-ghost" id="test-ssh">Проверить подключение</button>
          <button type="button" class="lp-btn" id="register-target">Сохранить</button>
        </div>
        <div class="lp-row" style="flex-wrap:wrap;gap:8px;">
          <button type="button" class="lp-btn lp-btn-ghost" id="load-swarm-catalog">Ansible catalog</button>
          <button type="button" class="lp-btn lp-btn-ghost" id="prepare-ansible">Скачать bundle (вручную)</button>
          <button type="button" class="lp-btn lp-btn-ghost" id="confirm-setup">Подтвердить настройку (если ставили вручную)</button>
          <span class="lp-muted" id="workspace-hint"></span>
        </div>
        <details style="margin-top:12px;">
          <summary class="lp-muted" style="cursor:pointer;font-size:13px;">Удалённая команда по SSH (агенты и вы)</summary>
          <p class="lp-muted" style="font-size:12px;margin:8px 0;">Только SSH-таргеты. Простые read-only команды выполняются сразу; остальные — ответ с токеном, затем отметьте подтверждение и выполните снова с тем же текстом команды.</p>
          <div class="lp-field"><label>Сервер (SSH)</label><select class="lp-select" id="ssh-exec-target" style="min-width:280px;"><option value="">— выберите —</option></select></div>
          <div class="lp-field"><label>Команда</label><textarea class="lp-textarea" id="ssh-exec-cmd" rows="2" placeholder="ls -la"></textarea></div>
          <input type="hidden" id="ssh-exec-token" value="" />
          <label class="lp-muted" style="font-size:13px;display:block;margin:6px 0;"><input type="checkbox" id="ssh-exec-confirmed" /> Я подтверждаю выполнение (если платформа запросила подтверждение)</label>
          <div class="lp-row" style="margin-top:8px;"><button type="button" class="lp-btn lp-btn-ghost" id="ssh-exec-run">Выполнить по SSH</button></div>
          <pre id="ssh-exec-out" class="lp-pre" style="max-height:240px;overflow:auto;margin-top:8px;font-size:11px;white-space:pre-wrap;word-break:break-word;"></pre>
        </details>
        <h3 style="margin:16px 0 8px 0;font-size:0.95rem;">Зарегистрированные серверы</h3>
        <p class="lp-muted" style="margin:0 0 8px 0;font-size:12px;">Статус агента по метрикам; установка — результат последней автоустановки (лог с сервера).</p>
        <div class="lp-row" style="margin-bottom:8px;">
          <button type="button" class="lp-btn lp-btn-ghost" id="refresh-targets-list">Обновить список</button>
        </div>
        <div id="targets-table-wrap" style="overflow:auto;">
          <table style="width:100%;border-collapse:collapse;font-size:13px;">
            <thead><tr>
              <th style="text-align:left;padding:6px;border-bottom:1px solid #e2e8f0;">Тип</th>
              <th style="text-align:left;padding:6px;border-bottom:1px solid #e2e8f0;">Host</th>
              <th style="text-align:left;padding:6px;border-bottom:1px solid #e2e8f0;">Port</th>
              <th style="text-align:left;padding:6px;border-bottom:1px solid #e2e8f0;">SSH user</th>
              <th style="text-align:left;padding:6px;border-bottom:1px solid #e2e8f0;">Установка</th>
              <th style="text-align:left;padding:6px;border-bottom:1px solid #e2e8f0;">Лог установки</th>
              <th style="text-align:left;padding:6px;border-bottom:1px solid #e2e8f0;">Метрик</th>
              <th style="text-align:left;padding:6px;border-bottom:1px solid #e2e8f0;">Агент</th>
              <th style="text-align:left;padding:6px;border-bottom:1px solid #e2e8f0;">Последняя метрика</th>
              <th style="text-align:left;padding:6px;border-bottom:1px solid #e2e8f0;"></th>
            </tr></thead>
            <tbody id="targets-table-body"><tr><td colspan="10" class="lp-muted">Выберите проект</td></tr></tbody>
          </table>
        </div>
      </div>
      </div>
      <div id="view-k8s-install" class="lp-portal-view">
      <div class="lp-panel">
        <h2>Kubernetes: установка на 3 сервера (Ansible)</h2>
        <p class="lp-muted">Сначала сохраните <strong>три</strong> SSH-таргета с <strong>одинаковым</strong> пользователем и <strong>одним и тем же</strong> ключом или паролем. Первый сервер — control plane, второй и третий — workers. Подходит для Ubuntu/Debian; установка может занять 15–40 минут.</p>
        <div class="lp-row" style="flex-wrap:wrap;gap:12px;align-items:flex-end;">
          <div class="lp-field"><label>Control plane</label><select class="lp-select" id="k8s-master-target" style="min-width:240px;"><option value="">— выберите SSH-таргет —</option></select></div>
          <div class="lp-field"><label>Worker 1</label><select class="lp-select" id="k8s-worker1-target" style="min-width:240px;"><option value="">— выберите SSH-таргет —</option></select></div>
          <div class="lp-field"><label>Worker 2</label><select class="lp-select" id="k8s-worker2-target" style="min-width:240px;"><option value="">— выберите SSH-таргет —</option></select></div>
          <div class="lp-field"><label>Pod network CIDR</label><input class="lp-input" id="k8s-pod-cidr" value="10.244.0.0/16" style="min-width:140px;" /></div>
        </div>
        <div class="lp-row" style="gap:8px;flex-wrap:wrap;align-items:center;">
          <button type="button" class="lp-btn" id="k8s-install-run">Запустить установку (поток лога)</button>
          <span class="lp-muted" id="k8s-install-status">—</span>
        </div>
        <pre id="k8s-install-log" class="lp-pre" style="max-height:300px;overflow:auto;margin-top:8px;font-size:11px;white-space:pre-wrap;word-break:break-word;"></pre>
      </div>
      </div>
      <div id="view-k8s" class="lp-portal-view">
      <div class="lp-panel">
        <h2>Kubernetes: управление</h2>
        <p class="lp-muted">Нужен <strong>таргет с kubeconfig</strong> (см. выше). Команды выполняются на платформе через kubectl; произвольный ввод запрещён — только выбранные операции.</p>
        <div class="lp-row" style="flex-wrap:wrap;gap:12px;align-items:flex-end;">
          <div class="lp-field"><label>Кластер</label><select class="lp-select" id="k8s-mgmt-target" style="min-width:260px;"><option value="">— сохраните kubeconfig —</option></select></div>
          <div class="lp-field"><label>Операция</label><select class="lp-select" id="k8s-mgmt-op" style="min-width:240px;"><option value="">— загрузка… —</option></select></div>
        </div>
        <div class="lp-row" style="flex-wrap:wrap;gap:12px;">
          <div class="lp-field"><label>namespace</label><input class="lp-input" id="k8s-mgmt-ns" placeholder="напр. default" style="max-width:160px;" /></div>
          <div class="lp-field"><label>name</label><input class="lp-input" id="k8s-mgmt-name" placeholder="pod / deployment" style="max-width:200px;" /></div>
          <div class="lp-field"><label>resource (describe)</label><input class="lp-input" id="k8s-mgmt-res" placeholder="pod, deployment…" style="max-width:140px;" /></div>
          <div class="lp-field"><label>container (logs)</label><input class="lp-input" id="k8s-mgmt-ctr" style="max-width:120px;" /></div>
          <div class="lp-field"><label>tail (logs)</label><input class="lp-input" id="k8s-mgmt-tail" type="number" value="200" style="max-width:80px;" /></div>
          <div class="lp-field"><label>replicas (scale)</label><input class="lp-input" id="k8s-mgmt-rep" type="number" placeholder="—" style="max-width:80px;" /></div>
        </div>
        <div class="lp-row" style="gap:12px;flex-wrap:wrap;align-items:center;">
          <label class="lp-muted" style="font-size:13px;"><input type="checkbox" id="k8s-mgmt-allns" /> все namespace (-A)</label>
          <label class="lp-muted" style="font-size:13px;"><input type="checkbox" id="k8s-mgmt-wide" /> wide</label>
          <button type="button" class="lp-btn" id="k8s-mgmt-run">Выполнить</button>
        </div>
        <pre id="k8s-mgmt-out" class="lp-pre" style="max-height:300px;overflow:auto;margin-top:8px;font-size:11px;white-space:pre-wrap;word-break:break-word;"></pre>
      </div>
      </div>
      <div id="view-infra" class="lp-portal-view">
      <div class="lp-panel">
        <h2>Инфраструктура и Ansible</h2>
        <p class="lp-muted">Ниже — опциональная раскатка GitLab/Nexus на ваших SSH-серверах через Ansible. Если используете свой GitLab или другие сервисы — добавьте их в блоке «Сервисы и учётки» (категория «Вручную»). «Только bundles» — скачать YAML локально; «Запустить Ansible на платформе» — playbook из adapter по сохранённому SSH.</p>
        <div class="lp-row" style="flex-wrap:wrap;gap:16px;">
          <div class="lp-field"><label>GitLab → сервер</label>
            <select class="lp-select" id="infra-gitlab-target" style="min-width:260px;"><option value="">— не назначен —</option></select>
          </div>
          <div class="lp-field"><label>Nexus → сервер</label>
            <select class="lp-select" id="infra-nexus-target" style="min-width:260px;"><option value="">— не назначен —</option></select>
          </div>
        </div>
        <div class="lp-row" style="flex-wrap:wrap;gap:12px;">
          <div class="lp-field"><label>GitLab HTTP порт</label><input class="lp-input" id="infra-gitlab-port" type="number" value="8081" style="max-width:100px;" /></div>
          <div class="lp-field"><label>Nexus HTTP порт</label><input class="lp-input" id="infra-nexus-port" type="number" value="8082" style="max-width:100px;" /></div>
        </div>
        <div class="lp-row" style="flex-wrap:wrap;gap:8px;">
          <button type="button" class="lp-btn lp-btn-ghost" id="infra-save-assignments">Сохранить назначения</button>
          <button type="button" class="lp-btn" id="infra-prepare-stack">Только bundles (YAML)</button>
          <button type="button" class="lp-btn" id="infra-run-stack">Запустить Ansible на платформе</button>
          <button type="button" class="lp-btn lp-btn-ghost" id="infra-load-context">Контекст разработки (JSON)</button>
          <button type="button" class="lp-btn" id="agent-demo-run" title="Нужен GitLab с PAT в ресурсах проекта">Демо агентов (GitLab)</button>
          <button type="button" class="lp-btn lp-btn-ghost" id="agents-bootstrap-planning" title="Повторно запустить planning (как ИИ-агент при создании проекта)">Planning (bootstrap)</button>
        </div>
        <div class="lp-panel" style="margin-top:10px;padding:12px;border:1px solid var(--lp-border);border-radius:12px;">
          <h3 style="margin:0 0 8px;font-size:0.95rem;">Ansible marketplace: запуск роли</h3>
          <p class="lp-muted" style="margin:0 0 8px;font-size:12px;">
            Выберите роль из каталога и SSH-таргет. Поля параметров строятся автоматически из vars_schema шаблона.
          </p>
          <div class="lp-row" style="flex-wrap:wrap;gap:10px;align-items:flex-end;">
            <div class="lp-field"><label>SSH-таргет</label>
              <select class="lp-select" id="infra-template-target" style="min-width:260px;">
                <option value="">— выберите SSH-сервер —</option>
              </select>
            </div>
            <div class="lp-field"><label>Роль (template)</label>
              <select class="lp-select" id="infra-template-id" style="min-width:320px;">
                <option value="">— загрузка каталога… —</option>
              </select>
            </div>
            <div class="lp-field">
              <button type="button" class="lp-btn lp-btn-ghost" id="infra-template-reload">Обновить каталог</button>
            </div>
          </div>
          <div id="infra-template-vars" class="lp-row" style="margin-top:8px;flex-wrap:wrap;gap:10px;"></div>
          <div class="lp-row" style="margin-top:8px;gap:12px;flex-wrap:wrap;align-items:center;">
            <label class="lp-muted" style="font-size:12px;display:flex;align-items:center;gap:6px;">
              <input type="checkbox" id="infra-template-fallback" /> fallback если роли нет
            </label>
            <div class="lp-field"><label>fallback service</label>
              <select class="lp-select" id="infra-template-fallback-service" style="min-width:160px;">
                <option value="gitlab">gitlab</option>
                <option value="docker">docker</option>
                <option value="nexus">nexus</option>
              </select>
            </div>
            <button type="button" class="lp-btn" id="infra-template-run">Запустить роль</button>
          </div>
          <p class="lp-muted" id="infra-template-hint" style="margin:8px 0 0;font-size:12px;"></p>
        </div>
        <details style="margin-top:10px;">
          <summary class="lp-muted" style="cursor:pointer;font-size:13px;">Песочница Python (sandbox)</summary>
          <p class="lp-muted" style="font-size:12px;margin:8px 0;">Код выполняется на сервисе sandbox с таймаутом; только для доверенных сценариев.</p>
          <textarea class="lp-textarea" id="sandbox-code" rows="3" style="width:100%;max-width:560px;font-family:monospace;font-size:12px;">print(sum(range(10)))</textarea>
          <div class="lp-row" style="margin-top:8px;">
            <button type="button" class="lp-btn lp-btn-ghost" id="sandbox-run">Выполнить в sandbox</button>
          </div>
        </details>
        <div class="lp-row" style="align-items:center;gap:8px;margin-top:10px;flex-wrap:wrap;">
          <span id="infra-ansible-status" class="lp-muted" style="font-size:13px;flex:1;min-width:220px;">Ansible: —</span>
        </div>
        <details id="infra-ansible-log-wrap" style="margin-top:8px;">
          <summary class="lp-muted" style="cursor:pointer;font-size:13px;">Полный лог Ansible (поток)</summary>
          <pre id="infra-ansible-log" class="lp-pre" style="max-height:360px;overflow:auto;margin-top:8px;font-size:11px;white-space:pre-wrap;word-break:break-word;"></pre>
        </details>
        <h3 style="margin:16px 0 8px 0;font-size:0.95rem;">Сервисы и учётки проекта</h3>
        <p class="lp-muted" style="margin:0 0 8px 0;font-size:12px;">
          Любые внешние сервисы (свой GitLab, Nexus, реестр и т.д.): имя, адрес, пользователь, пароль или API-ключ.
          Записи, которые подтягивает Ansible после раскатки GitLab/Nexus на сервер, попадают сюда же. Отключённые строки не попадают в контекст для ИИ-агентов.
          Секреты хранятся зашифрованно.
        </p>
        <div class="lp-panel" style="margin-bottom:12px;padding:12px;border:1px solid var(--lp-border);border-radius:12px;">
          <div class="lp-row" style="flex-wrap:wrap;gap:10px;align-items:flex-end;margin-bottom:8px;">
            <div class="lp-field"><label for="infra-service-name">Название</label>
              <input class="lp-input" id="infra-service-name" placeholder="Мой GitLab" style="min-width:160px;" /></div>
            <div class="lp-field"><label for="infra-service-host">IP / FQDN</label>
              <input class="lp-input" id="infra-service-host" placeholder="git.company.com" style="min-width:180px;" /></div>
            <div class="lp-field"><label for="infra-service-user">Пользователь</label>
              <input class="lp-input" id="infra-service-user" placeholder="root / token-id" style="min-width:140px;" /></div>
            <div class="lp-field"><label for="infra-service-secret">Пароль / API key</label>
              <input class="lp-input" id="infra-service-secret" type="password" placeholder="•••" autocomplete="off" style="min-width:160px;" /></div>
            <label class="lp-muted" style="font-size:13px;display:flex;align-items:center;gap:6px;">
              <input type="checkbox" id="infra-service-enabled" checked /> включён
            </label>
          </div>
          <div class="lp-row" style="gap:8px;flex-wrap:wrap;align-items:center;">
            <button type="button" class="lp-btn" id="infra-service-save">Добавить сервис</button>
            <button type="button" class="lp-btn lp-btn-ghost" id="infra-service-cancel-edit" style="display:none;">Отменить правку</button>
            <input type="hidden" id="infra-service-edit-id" value="" />
            <button type="button" class="lp-btn lp-btn-ghost" id="infra-refresh-resources">Обновить таблицу</button>
          </div>
          <p class="lp-muted" id="infra-service-form-hint" style="margin:8px 0 0;font-size:12px;display:none;"></p>
        </div>
        <div id="infra-resources-wrap" style="overflow:auto;">
          <table style="width:100%;border-collapse:collapse;font-size:13px;">
            <thead><tr>
              <th style="text-align:left;padding:6px;border-bottom:1px solid #e2e8f0;">Название</th>
              <th style="text-align:left;padding:6px;border-bottom:1px solid #e2e8f0;">IP / FQDN</th>
              <th style="text-align:left;padding:6px;border-bottom:1px solid #e2e8f0;">Пользователь</th>
              <th style="text-align:left;padding:6px;border-bottom:1px solid #e2e8f0;">Секрет</th>
              <th style="text-align:left;padding:6px;border-bottom:1px solid #e2e8f0;">Вкл</th>
              <th style="text-align:left;padding:6px;border-bottom:1px solid #e2e8f0;">Источник</th>
              <th style="text-align:left;padding:6px;border-bottom:1px solid #e2e8f0;"></th>
            </tr></thead>
            <tbody id="infra-resources-body"><tr><td colspan="7" class="lp-muted">Выберите проект</td></tr></tbody>
          </table>
        </div>
      </div>
      </div>
      <div id="view-service" class="lp-portal-view">
      <div class="lp-panel">
        <h2>Журнал действий</h2>
        <p class="lp-muted" style="margin:0 0 8px 0;font-size:12px;">Операции по проекту (Ansible, серверы, назначения) в ClickHouse. После «Запустить Ansible» обновите список — в деталях будут stdout/stderr.</p>
        <div class="lp-row" style="margin-bottom:8px;flex-wrap:wrap;gap:8px;align-items:center;">
          <button type="button" class="lp-btn lp-btn-ghost" id="activity-logs-refresh">Обновить журнал</button>
          <span class="lp-muted" id="activity-logs-hint" style="font-size:12px;"></span>
        </div>
        <div id="activity-logs-wrap" style="overflow:auto;">
          <table style="width:100%;border-collapse:collapse;font-size:13px;">
            <thead><tr>
              <th style="text-align:left;padding:6px;border-bottom:1px solid #e2e8f0;">Время</th>
              <th style="text-align:left;padding:6px;border-bottom:1px solid #e2e8f0;">Действие</th>
              <th style="text-align:left;padding:6px;border-bottom:1px solid #e2e8f0;">Статус</th>
              <th style="text-align:left;padding:6px;border-bottom:1px solid #e2e8f0;">Кратко</th>
              <th style="text-align:left;padding:6px;border-bottom:1px solid #e2e8f0;">Кто</th>
            </tr></thead>
            <tbody id="activity-logs-body"><tr><td colspan="5" class="lp-muted">Выберите проект</td></tr></tbody>
          </table>
        </div>
        <details style="margin-top:10px;">
          <summary class="lp-muted" style="cursor:pointer;font-size:13px;">Детали записи (JSON)</summary>
          <pre class="lp-pre" id="activity-logs-detail" style="max-height:280px;overflow:auto;margin-top:8px;font-size:11px;"></pre>
        </details>
      </div>
      <div class="lp-panel">
        <h2>Отладочный вывод</h2>
        <pre class="lp-pre" id="portal-output"></pre>
      </div>
      </div>
      </div>
      </div>
      `,
    }),
  );

  const output = document.getElementById("portal-output");
  const tokenInput = document.getElementById("user-token") as HTMLInputElement | null;
  const SELECTED_PROJECT_STORAGE_KEY = "lambot_selected_project_id";
  const readSelectedProjectId = (): string => {
    try {
      return String(sessionStorage.getItem(SELECTED_PROJECT_STORAGE_KEY) || "").trim();
    } catch {
      return "";
    }
  };
  const writeSelectedProjectId = (projectId: string) => {
    try {
      const id = String(projectId || "").trim();
      if (id) {
        sessionStorage.setItem(SELECTED_PROJECT_STORAGE_KEY, id);
      } else {
        sessionStorage.removeItem(SELECTED_PROJECT_STORAGE_KEY);
      }
    } catch {
      /* unavailable storage */
    }
  };
  let currentProjectId = readSelectedProjectId();
  let currentTargetId = "";
  let statusPoll: number | null = null;
  let lastActivityItems: Array<Record<string, unknown>> = [];
  let currentDocId = "";
  type InfraTemplateCatalogItem = {
    id: string;
    name?: string;
    description?: string;
    tags?: string[];
    vars_schema?: Record<string, { type?: string; required?: boolean }>;
  };
  let infraTemplateCatalog: InfraTemplateCatalogItem[] = [];
  let infraTemplateSchema: Record<string, { type?: string; required?: boolean }> = {};
  let infraTemplateCatalogProjectId = "";

  /** Основной чат: пользователь, одиночный ассистент (LangGraph), либо сообщения ролей ИИ Команды. */
  type ProjectChatMsg =
    | { role: "user"; content: string }
    | { role: "assistant"; content: string }
    | { role: "team"; content: string; teamLabel: string; taskTitle?: string; stepKey?: string };

  let projectAiChatHistory: ProjectChatMsg[] = [];

  const orchChatSyncedStorageKey = (projectId: string, runId: string) =>
    `lambot_orch_chat_sync_${projectId}_${runId}`;

  const readOrchChatSyncedKeys = (projectId: string, runId: string): Set<string> => {
    try {
      const raw = sessionStorage.getItem(orchChatSyncedStorageKey(projectId, runId));
      if (!raw) return new Set();
      const a = JSON.parse(raw) as unknown;
      if (!Array.isArray(a)) return new Set();
      return new Set(a.filter((x): x is string => typeof x === "string"));
    } catch {
      return new Set();
    }
  };

  const writeOrchChatSyncedKeys = (projectId: string, runId: string, keys: Set<string>) => {
    try {
      sessionStorage.setItem(orchChatSyncedStorageKey(projectId, runId), JSON.stringify([...keys]));
    } catch {
      /* quota */
    }
  };

  const loadProjectAiChatForProject = (pid: string) => {
    if (!pid) {
      projectAiChatHistory = [];
      return;
    }
    try {
      const raw = sessionStorage.getItem(projectAiChatStorageKey(pid));
      if (!raw) {
        projectAiChatHistory = [];
        return;
      }
      const parsed = JSON.parse(raw) as unknown;
      if (!Array.isArray(parsed)) {
        projectAiChatHistory = [];
        return;
      }
      projectAiChatHistory = parsed
        .filter((x) => x && typeof x === "object")
        .map((x) => {
          const o = x as Record<string, unknown>;
          if (o.role === "team" && typeof o.teamLabel === "string") {
            const content = String(o.content ?? "").slice(0, 120000);
            if (!content) return null;
            return {
              role: "team" as const,
              content,
              teamLabel: o.teamLabel,
              taskTitle: typeof o.taskTitle === "string" ? o.taskTitle : undefined,
              stepKey: typeof o.stepKey === "string" ? o.stepKey : undefined,
            };
          }
          const role = o.role === "assistant" ? "assistant" : "user";
          const content = String(o.content ?? "").slice(0, 120000);
          return { role, content };
        })
        .filter((x): x is ProjectChatMsg => x != null && x.content.length > 0);
    } catch {
      projectAiChatHistory = [];
    }
  };

  const saveProjectAiChat = () => {
    if (!currentProjectId) return;
    try {
      sessionStorage.setItem(projectAiChatStorageKey(currentProjectId), JSON.stringify(projectAiChatHistory));
    } catch {
      /* quota */
    }
  };

  const renderProjectAiChat = () => {
    const thread = document.getElementById("project-ai-chat-thread");
    const gate = document.getElementById("project-ai-chat-gate");
    if (!thread) return;
    if (!currentProjectId) {
      thread.innerHTML = `<p class="lp-muted">Выберите проект, чтобы начать диалог.</p>`;
      if (gate) gate.textContent = "";
      return;
    }
    if (gate) gate.textContent = "";
    if (projectAiChatHistory.length === 0) {
      thread.innerHTML = `<p class="lp-muted lp-ai-chat-placeholder">Опишите идею продукта или задайте вопрос по проекту. Ниже — ответ ассистента; при запуске «ИИ Команда» в этом же чате по очереди появятся сообщения от ролей (маршрутизация, PM, архитектор и т.д.).</p>`;
      return;
    }
    thread.innerHTML = projectAiChatHistory
      .map((m) => {
        if (m.role === "user") {
          return `<div class="lp-ai-msg lp-ai-msg-user"><div class="lp-ai-msg-inner">${escapeHtml(m.content)}</div></div>`;
        }
        if (m.role === "assistant") {
          return `<div class="lp-ai-msg lp-ai-msg-assistant"><div class="lp-ai-msg-inner">${escapeHtml(m.content)}</div></div>`;
        }
        const task = m.taskTitle ? `<span class="lp-ai-team-task">${escapeHtml(m.taskTitle)}</span>` : "";
        return `<div class="lp-ai-msg lp-ai-msg-team"><div class="lp-ai-msg-team-wrap"><div class="lp-ai-team-head"><span class="lp-ai-team-badge">${escapeHtml(m.teamLabel)}</span>${task}</div><div class="lp-ai-msg-inner lp-ai-msg-team-body">${escapeHtml(m.content)}</div></div></div>`;
      })
      .join("");
    thread.scrollTop = thread.scrollHeight;
  };

  const setProjectAiChatBusy = (busy: boolean) => {
    const inp = document.getElementById("project-ai-chat-input") as HTMLTextAreaElement | null;
    const btn = document.getElementById("project-ai-chat-send") as HTMLButtonElement | null;
    const st = document.getElementById("project-ai-chat-status");
    if (inp) inp.disabled = busy || !currentProjectId;
    if (btn) btn.disabled = busy || !currentProjectId;
    if (st) st.textContent = busy ? "Ответ…" : "";
  };

  const token = () => getPortalToken();
  const write = (payload: unknown) => {
    if (output) output.textContent = JSON.stringify(payload, null, 2);
  };
  const isFetchNetworkError = (e: unknown) => {
    const s = String(e || "");
    return s.includes("Failed to fetch") || s.includes("NetworkError") || s.includes("TypeError: network error");
  };
  const fetchNetworkHint = (operation: string) =>
    `Сетевой сбой (${operation}): браузер не смог достучаться до API. Обычно это перезапуск/недоступность api-gateway, обрыв сети или блокировка прокси/CORS. Проверьте сервисы docker compose и попробуйте повторить.`;

  let orchPollTimer: number | null = null;
  let orchStreamAbort: AbortController | null = null;
  let orchStreamLastRun: OrchRunData | null = null;
  let orchBrowserLogsTs = 0;
  let orchModalStepKey = "";
  let orchModalRunId = "";
  let orchModalReasoningMode: "brief" | "detailed" = "brief";
  let docsMdEditor: ReturnType<typeof mountDocsMarkdownEditor> | null = null;
  const orchLiveLogMax = 180_000;
  const orchStateMax = 260_000;

  const stopOrchestrationPoll = () => {
    if (orchPollTimer !== null) {
      window.clearInterval(orchPollTimer);
      orchPollTimer = null;
    }
  };

  const stopOrchestrationStream = () => {
    if (orchStreamAbort) {
      try {
        orchStreamAbort.abort();
      } catch {
        /* ignore */
      }
      orchStreamAbort = null;
    }
  };

  const appendOrchestrationLiveLog = (line: string) => {
    const pre = document.getElementById("orchestration-live-log");
    if (!pre) return;
    const ts = new Date().toLocaleTimeString();
    let txt = (pre.textContent || "") + `[${ts}] ${line}\n`;
    if (txt.length > orchLiveLogMax) {
      txt = `… [обрезано]\n${txt.slice(-orchLiveLogMax)}`;
    }
    pre.textContent = txt;
    pre.scrollTop = pre.scrollHeight;
  };

  const resetOrchestrationLiveLog = () => {
    const pre = document.getElementById("orchestration-live-log");
    if (pre) pre.textContent = "";
    orchStreamLastRun = null;
    orchBrowserLogsTs = 0;
  };

  const renderOrchestrationStateLive = (data: OrchRunData | null) => {
    const pre = document.getElementById("orchestration-state-json");
    if (!pre) return;
    if (!data) {
      pre.textContent = "";
      return;
    }
    let pretty = "";
    try {
      pretty = JSON.stringify(data, null, 2);
    } catch {
      pretty = String(data);
    }
    if (pretty.length > orchStateMax) {
      pretty = `${pretty.slice(0, orchStateMax)}\n... [truncated]`;
    }
    pre.textContent = pretty;
    pre.scrollTop = pre.scrollHeight;
  };

  type OrchStep = {
    step_key?: string;
    role_label?: string;
    task_title?: string;
    label?: string;
    task?: string;
    status: string;
    output?: string | null;
    error?: string | null;
    started_at?: string | null;
    finished_at?: string | null;
    structured?: Record<string, unknown> | null;
  };

  type OrchRunData = {
    run_id?: string;
    status?: string;
    steps?: OrchStep[];
    architecture_md?: string | null;
    final_report_md?: string | null;
    final_message?: string | null;
    error?: string | null;
    phase?: string | null;
    iteration?: number;
    batch_status?: string;
    execution_tasks?: Array<Record<string, unknown>>;
  };

  type BrowserLogItem = {
    id?: string;
    created_at?: string;
    project_id?: string;
    run_id?: string | null;
    session_id?: string;
    action?: string;
    ok?: boolean;
    url?: string;
    artifact_name?: string;
    error?: string;
    details?: Record<string, unknown>;
  };

  type ProjectDocItem = {
    id: string;
    slug: string;
    title: string;
    doc_type: string;
    status: string;
    latest_version: number;
    updated_at?: string;
  };

  type GraphNodeDef = {
    key: string;
    label: string;
    lane: number;
    order: number;
  };

  const ORCH_GRAPH_NODES: GraphNodeDef[] = [
    { key: "intake", label: "Маршрутизация", lane: 0, order: 0 },
    { key: "pm", label: "PM", lane: 1, order: 0 },
    { key: "architect", label: "Архитектор", lane: 2, order: 0 },
    { key: "devops_plan", label: "DevOps", lane: 3, order: 0 },
    { key: "backend_plan", label: "Backend", lane: 3, order: 1 },
    { key: "frontend_plan", label: "Frontend", lane: 3, order: 2 },
    { key: "implementation", label: "Реализация", lane: 4, order: 0 },
    { key: "deploy", label: "Деплой", lane: 5, order: 0 },
    { key: "qa_validation", label: "QA", lane: 6, order: 0 },
    { key: "pm_summary", label: "PM итог", lane: 7, order: 0 },
  ];
  const ORCH_LAYER_KEYS: string[][] = [
    ["intake"],
    ["pm"],
    ["architect"],
    ["devops_plan", "backend_plan", "frontend_plan"],
    ["implementation"],
    ["deploy"],
    ["qa_validation"],
    ["pm_summary"],
  ];
  const ORCH_GRAPH_EDGES: Array<[string, string]> = [
    ["intake", "pm"],
    ["pm", "architect"],
    ["architect", "devops_plan"],
    ["architect", "backend_plan"],
    ["architect", "frontend_plan"],
    ["devops_plan", "implementation"],
    ["backend_plan", "implementation"],
    ["frontend_plan", "implementation"],
    ["implementation", "deploy"],
    ["deploy", "qa_validation"],
    ["qa_validation", "pm_summary"],
  ];

  const orchStatusLabel = (raw: string | undefined): string => {
    const v = (raw || "").trim().toLowerCase();
    if (v === "done") return "Готово";
    if (v === "running") return "В работе";
    if (v === "error") return "Ошибка";
    if (v === "skipped") return "Пропущено";
    if (v === "paused") return "Ждём уточнений";
    return "Ожидание";
  };

  const orchBadgeClass = (raw: string | undefined): string => {
    const v = (raw || "").trim().toLowerCase();
    if (v === "done") return "lp-orch-badge ok";
    if (v === "running") return "lp-orch-badge run";
    if (v === "error") return "lp-orch-badge err";
    if (v === "skipped") return "lp-orch-badge skip";
    if (v === "paused") return "lp-orch-badge run";
    return "lp-orch-badge wait";
  };

  const fmtTs = (iso?: string | null): string => {
    if (!iso) return "—";
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString();
  };

  const buildStepMap = (steps: OrchStep[] | undefined): Record<string, OrchStep> => {
    const out: Record<string, OrchStep> = {};
    for (const s of steps || []) {
      const key = (s.step_key || "").trim();
      if (!key) continue;
      out[key] = s;
    }
    return out;
  };

  const peersForStep = (stepKey: string, stepMap: Record<string, OrchStep>): string[] => {
    const layer = ORCH_LAYER_KEYS.find((x) => x.includes(stepKey));
    if (!layer || layer.length < 2) return [];
    return layer
      .filter((k) => k !== stepKey)
      .map((k) => {
        const s = stepMap[k];
        const lbl = ORCH_GRAPH_NODES.find((n) => n.key === k)?.label || k;
        const st = orchStatusLabel(s?.status);
        return `${lbl}: ${st}`;
      });
  };

  const openOrchNodeModal = (stepKey: string, data: OrchRunData) => {
    const root = document.getElementById("orchestration-node-modal");
    if (!root) return;
    const titleEl = document.getElementById("orch-node-modal-title");
    const bodyEl = document.getElementById("orch-node-modal-body");
    const stepMap = buildStepMap(data.steps);
    const step = stepMap[stepKey];
    const node = ORCH_GRAPH_NODES.find((n) => n.key === stepKey);
    const title = node?.label || step?.role_label || stepKey;
    if (titleEl) titleEl.textContent = title;
    const peers = peersForStep(stepKey, stepMap);
    const task = step?.task_title || step?.task || "—";
    const output = step?.output ? String(step.output).trim() : "";
    const err = step?.error ? String(step.error).trim() : "";
    const stObj = (step?.structured && typeof step.structured === "object"
      ? (step.structured as Record<string, unknown>)
      : {}) as Record<string, unknown>;
    const attempt = Number(stObj.attempt || 0);
    const maxAttempts = Number(stObj.max_attempts || 0);
    const envelope = stObj.step_envelope && typeof stObj.step_envelope === "object"
      ? (stObj.step_envelope as Record<string, unknown>)
      : null;
    const envelopeSummary = envelope ? String(envelope.summary_md || "").trim() : "";
    const envelopeStatus = envelope ? String(envelope.step_status || "").trim() : "";
    const envelopeContinue =
      envelope && typeof envelope.pipeline_continue === "boolean"
        ? Boolean(envelope.pipeline_continue)
        : null;
    const envelopePeerQueries = envelope && Array.isArray(envelope.peer_queries)
      ? envelope.peer_queries
          .filter((x) => typeof x === "object" && x !== null)
          .map((x) => x as Record<string, unknown>)
      : [];
    const qfu = Array.isArray(stObj.questions_for_user)
      ? stObj.questions_for_user.map((x) => String(x)).filter(Boolean)
      : [];
    const peerResp = Array.isArray(stObj.peer_responses)
      ? stObj.peer_responses
          .filter((x) => typeof x === "object" && x !== null)
          .map((x) => x as Record<string, unknown>)
      : [];
    const qaGate = String(stObj.qa_gate || "");
    const liveAction: string[] = [];
    if ((step?.status || "") === "running") {
      liveAction.push("Выполняет текущую задачу шага");
    }
    if (attempt > 0 && maxAttempts > 0) {
      liveAction.push(`Попытка ${attempt}/${maxAttempts}`);
    }
    if (envelope) {
      const ss = String(envelope.step_status || "");
      if (ss) liveAction.push(`Step status: ${ss}`);
    }
    if (qfu.length) liveAction.push(`Ждёт уточнений пользователя: ${qfu.length}`);
    if (peerResp.length) liveAction.push(`Синхронизация с коллегами: ${peerResp.length}`);
    if (qaGate === "no_go") liveAction.push("QA gate: NO_GO (деплой блокирован)");
    if (!liveAction.length) liveAction.push("Явных действий в structured-мета нет");
    const reasoningBrief: string[] = [];
    const reasoningDetailed: string[] = [];
    if ((step?.status || "") === "running") {
      reasoningBrief.push("Шаг активен.");
      reasoningDetailed.push("Шаг активен: агент выполняет текущую задачу.");
    }
    if (attempt > 0 && maxAttempts > 0) {
      reasoningBrief.push(`Повторная попытка ${attempt}/${maxAttempts}.`);
      reasoningDetailed.push(`Повторная попытка: ${attempt}/${maxAttempts}.`);
    }
    if (envelopeStatus) {
      const statusLabel =
        envelopeStatus === "need_user_clarification"
          ? "нужно уточнение от пользователя"
          : envelopeStatus === "need_peer_sync"
            ? "нужна синхронизация с коллегами"
            : "шаг завершен";
      reasoningBrief.push(`Решение шага: ${statusLabel}.`);
      reasoningDetailed.push(`Решение шага: ${statusLabel}.`);
    }
    if (envelopeContinue !== null) {
      reasoningBrief.push(`Продолжение пайплайна: ${envelopeContinue ? "да" : "нет"}.`);
      reasoningDetailed.push(`Продолжение пайплайна: ${envelopeContinue ? "да" : "нет"}.`);
    }
    if (envelopePeerQueries.length) {
      reasoningBrief.push(`Запросы к коллегам: ${envelopePeerQueries.length}.`);
      const peerQueryRows = envelopePeerQueries
        .map((x, idx) => {
          const role = String(x.role || "").trim() || "роль";
          const question = String(x.question || "").replace(/\s+/g, " ").trim();
          return `   ${idx + 1}. запрос к ${role}: ${question || "без текста"}`;
        })
        .join("\n");
      reasoningDetailed.push(`Запросы к коллегам:\n${peerQueryRows}`);
    }
    if (peerResp.length) {
      reasoningBrief.push(`Ответы коллег: ${peerResp.length}.`);
      const peerRespRows = peerResp
        .map((x, idx) => {
          const role = String(x.role || "").trim() || "роль";
          const answerRaw = String(x.answer || "").replace(/\s+/g, " ").trim();
          const answer = answerRaw.length > 280 ? `${answerRaw.slice(0, 280)}…` : answerRaw;
          return `   ${idx + 1}. ответ ${role}: ${answer || "пусто"}`;
        })
        .join("\n");
      reasoningDetailed.push(`Полученные ответы:\n${peerRespRows}`);
    }
    if (qfu.length) {
      reasoningBrief.push(`Нужны уточнения пользователя: ${qfu.length}.`);
      const qRows = qfu.map((x, idx) => `   ${idx + 1}. ${String(x)}`).join("\n");
      reasoningDetailed.push(`Что нужно от пользователя:\n${qRows}`);
    }
    if (qaGate === "no_go") {
      reasoningBrief.push("QA gate: NO_GO.");
      reasoningDetailed.push("QA gate: NO_GO — релиз заблокирован до исправлений.");
    }
    if (envelopeSummary) {
      reasoningBrief.push("Есть краткое обоснование шага.");
      const summary = envelopeSummary.length > 1200 ? `${envelopeSummary.slice(0, 1200)}\n…` : envelopeSummary;
      reasoningDetailed.push(`Краткое обоснование шага:\n${summary}`);
    }
    if (!reasoningBrief.length) {
      reasoningBrief.push("Явный structured-трейс пока не сформирован.");
    }
    if (!reasoningDetailed.length) {
      reasoningDetailed.push("Явный structured-трейс пока не сформирован.");
    }
    const reasoningText = orchModalReasoningMode === "brief"
      ? reasoningBrief.join("\n")
      : reasoningDetailed.join("\n\n");
    const briefBtnClass = orchModalReasoningMode === "brief" ? "lp-btn" : "lp-btn lp-btn-ghost";
    const detailedBtnClass = orchModalReasoningMode === "detailed" ? "lp-btn" : "lp-btn lp-btn-ghost";
    const outputLines = (output || "").split(/\r?\n/).filter((x) => x.trim());
    const outputTail = outputLines.slice(-8).join("\n");
    const execRows =
      stepKey === "execution_plan" && Array.isArray(data.execution_tasks)
        ? data.execution_tasks
            .map((t) => {
              const title = String(t.title || "task");
              const kind = String(t.action_kind || "action");
              const st = String(t.status || "pending");
              const er = t.error ? ` · err: ${String(t.error)}` : "";
              return `${title} (${kind}) — ${st}${er}`;
            })
            .join("\n")
        : "";
    if (bodyEl) {
      bodyEl.innerHTML = `
        <div class="lp-orch-modal-grid">
          <div><b>Статус:</b> ${escapeHtml(orchStatusLabel(step?.status))}</div>
          <div><b>Шаг:</b> <code>${escapeHtml(stepKey)}</code></div>
          <div><b>Задача:</b> ${escapeHtml(task)}</div>
          <div><b>Старт:</b> ${escapeHtml(fmtTs(step?.started_at))}</div>
          <div><b>Финиш:</b> ${escapeHtml(fmtTs(step?.finished_at))}</div>
          <div><b>Фаза run:</b> ${escapeHtml(String(data.phase || "—"))}</div>
        </div>
        <div class="lp-orch-modal-block">
          <h4>С кем работает</h4>
          <p class="lp-muted">${peers.length ? escapeHtml(peers.join(" · ")) : "Этот шаг не имеет параллельных коллег в текущем DAG-слое."}</p>
        </div>
        <div class="lp-orch-modal-block">
          <h4>Чем занят агент сейчас</h4>
          <pre class="lp-orch-pre">${escapeHtml(liveAction.join("\n"))}</pre>
        </div>
        <div class="lp-orch-modal-block">
          <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;">
            <h4 style="margin:0;">Ход рассуждений</h4>
            <div style="display:flex;gap:6px;">
              <button type="button" id="orch-reasoning-brief" class="${briefBtnClass}" style="padding:4px 8px;font-size:12px;">Кратко</button>
              <button type="button" id="orch-reasoning-detailed" class="${detailedBtnClass}" style="padding:4px 8px;font-size:12px;">Подробно</button>
            </div>
          </div>
          <div class="lp-markdown lp-orch-pre">${renderMarkdownHtml(reasoningText)}</div>
        </div>
        <div class="lp-orch-modal-block">
          <h4>Над чем работает</h4>
          <div class="lp-markdown lp-orch-pre">${renderMarkdownHtml(outputTail || "Пока нет live-вывода шага")}</div>
        </div>
        ${
          execRows
            ? `<div class="lp-orch-modal-block"><h4>Текущие задачи исполнения</h4><pre class="lp-orch-pre">${escapeHtml(execRows)}</pre></div>`
            : ""
        }
        <div class="lp-orch-modal-block">
          <h4>Ошибка</h4>
          <pre class="lp-orch-pre lp-error">${escapeHtml(err || "Нет")}</pre>
        </div>
        <div class="lp-orch-modal-block">
          <h4>Результат шага</h4>
          <div class="lp-markdown lp-orch-pre">${renderMarkdownHtml(output || "Пока нет вывода")}</div>
        </div>
      `;
      const briefBtn = bodyEl.querySelector("#orch-reasoning-brief");
      const detailedBtn = bodyEl.querySelector("#orch-reasoning-detailed");
      briefBtn?.addEventListener("click", () => {
        orchModalReasoningMode = "brief";
        openOrchNodeModal(stepKey, data);
      });
      detailedBtn?.addEventListener("click", () => {
        orchModalReasoningMode = "detailed";
        openOrchNodeModal(stepKey, data);
      });
    }
    orchModalStepKey = stepKey;
    orchModalRunId = String(data.run_id || "");
    root.removeAttribute("hidden");
    document.body.classList.add("lp-modal-open");
  };

  const closeOrchNodeModal = () => {
    const root = document.getElementById("orchestration-node-modal");
    if (!root) return;
    root.setAttribute("hidden", "hidden");
    document.body.classList.remove("lp-modal-open");
    orchModalStepKey = "";
    orchModalRunId = "";
  };

  const renderOrchestrationGraph = (data: OrchRunData | null) => {
    const wrap = document.getElementById("orchestration-graph-wrap");
    if (!wrap) return;
    if (!data) {
      wrap.innerHTML = `<p class="lp-muted">Граф появится после запуска оркестрации.</p>`;
      return;
    }
    const stepMap = buildStepMap(data.steps);
    const layerX = 176;
    const rowY = 112;
    const nodeW = 154;
    const nodeH = 72;
    const padX = 22;
    const padY = 22;
    const maxRow = Math.max(...ORCH_GRAPH_NODES.map((n) => n.order));
    const width = padX * 2 + layerX * (ORCH_LAYER_KEYS.length - 1) + nodeW;
    const height = padY * 2 + rowY * maxRow + nodeH;
    const positions: Record<string, { x: number; y: number }> = {};
    for (const n of ORCH_GRAPH_NODES) {
      positions[n.key] = { x: padX + n.lane * layerX, y: padY + n.order * rowY };
    }
    const edgesSvg = ORCH_GRAPH_EDGES.map(([from, to]) => {
      const a = positions[from];
      const b = positions[to];
      if (!a || !b) return "";
      const stFrom = stepMap[from]?.status || "pending";
      const stTo = stepMap[to]?.status || "pending";
      const active = stFrom === "running" || stTo === "running";
      const done = stFrom === "done" && stTo === "done";
      const cls = active ? "edge edge-active" : done ? "edge edge-done" : "edge";
      const x1 = a.x + nodeW;
      const y1 = a.y + nodeH / 2;
      const x2 = b.x;
      const y2 = b.y + nodeH / 2;
      return `<path class="${cls}" d="M ${x1} ${y1} C ${x1 + 40} ${y1}, ${x2 - 40} ${y2}, ${x2} ${y2}" />`;
    }).join("");
    const nodesHtml = ORCH_GRAPH_NODES.map((n) => {
      const st = stepMap[n.key]?.status || "pending";
      const pos = positions[n.key];
      const peers = peersForStep(n.key, stepMap);
      const peerHint = peers.length ? `В связке: ${peers.join(" · ")}` : "Последовательный шаг";
      return `
      <button type="button" class="lp-orch-node status-${escapeHtml(st)}" data-step-key="${escapeHtml(n.key)}"
        title="${escapeHtml(peerHint)}"
        style="left:${pos.x}px;top:${pos.y}px;width:${nodeW}px;height:${nodeH}px;">
        <span class="${orchBadgeClass(st)}">${escapeHtml(orchStatusLabel(st))}</span>
        <span class="lp-orch-node-title">${escapeHtml(n.label)}</span>
      </button>`;
    }).join("");
    wrap.innerHTML = `
      <div class="lp-orch-graph-scroll">
        <div class="lp-orch-graph-canvas" style="width:${width}px;height:${height}px;">
          <svg class="lp-orch-graph-svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">${edgesSvg}</svg>
          <div class="lp-orch-graph-nodes" style="width:${width}px;height:${height}px;">${nodesHtml}</div>
        </div>
      </div>
      <p class="lp-muted" style="margin:8px 0 0;font-size:12px;">Нажмите на узел, чтобы открыть детали шага и связи с другими агентами.</p>
    `;
    wrap.querySelectorAll<HTMLButtonElement>(".lp-orch-node").forEach((btn) => {
      btn.addEventListener("click", () => {
        const k = (btn.dataset.stepKey || "").trim();
        if (!k) return;
        openOrchNodeModal(k, data);
      });
    });
  };

  const renderBrowserLogsPanel = (items: BrowserLogItem[], runId: string) => {
    const wrap = document.getElementById("browser-logs-wrap");
    const st = document.getElementById("browser-logs-status-line");
    if (!wrap) return;
    if (st) st.textContent = `run ${runId || "—"} · записей ${items.length}`;
    if (!items.length) {
      wrap.innerHTML = `<p class="lp-muted">Пока нет browser-действий для этого прогона.</p>`;
      return;
    }
    wrap.innerHTML = items
      .map((it) => {
        const action = escapeHtml(String(it.action || "action"));
        const ok = Boolean(it.ok);
        const badge = ok ? "lp-orch-badge ok" : "lp-orch-badge err";
        const ts = fmtTs(it.created_at || null);
        const sid = escapeHtml(String(it.session_id || "—"));
        const url = escapeHtml(String(it.url || "—"));
        const err = it.error ? `<pre class="lp-orch-pre lp-error">${escapeHtml(String(it.error))}</pre>` : "";
        const artifact = String(it.artifact_name || "").trim();
        const openBtn = artifact
          ? `<button type="button" class="lp-btn lp-btn-ghost lp-browser-open-artifact" data-name="${escapeHtml(artifact)}">Открыть скриншот</button>`
          : "";
        return `<article class="lp-orch-card">
          <div class="lp-orch-card-head">
            <span class="${badge}">${action}</span>
            <span class="lp-muted" style="font-size:12px;">${escapeHtml(ts)}</span>
          </div>
          <p class="lp-muted" style="font-size:12px;margin:0;">session: <code>${sid}</code></p>
          <p class="lp-muted" style="font-size:12px;margin:4px 0 8px;word-break:break-all;">url: ${url}</p>
          ${openBtn}
          ${err}
        </article>`;
      })
      .join("");
    wrap.querySelectorAll<HTMLButtonElement>(".lp-browser-open-artifact").forEach((btn) => {
      btn.addEventListener("click", async () => {
        if (!currentProjectId) return;
        const name = (btn.dataset.name || "").trim();
        if (!name) return;
        btn.disabled = true;
        try {
          const r = await fetch(
            `${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/browser/artifacts/${encodeURIComponent(name)}`,
            { headers: authHeader(token()) },
          );
          if (!r.ok) {
            const tx = await r.text();
            alert(tx || String(r.status));
            return;
          }
          const blob = await r.blob();
          const obj = URL.createObjectURL(blob);
          window.open(obj, "_blank", "noopener,noreferrer");
          setTimeout(() => URL.revokeObjectURL(obj), 60000);
        } catch (e) {
          alert(String(e));
        } finally {
          btn.disabled = false;
        }
      });
    });
  };

  const loadBrowserLogs = async (runId: string) => {
    const wrap = document.getElementById("browser-logs-wrap");
    const st = document.getElementById("browser-logs-status-line");
    if (!wrap || !currentProjectId || !runId) {
      if (wrap) wrap.innerHTML = `<p class="lp-muted">Логи браузера появятся после запуска оркестрации.</p>`;
      if (st) st.textContent = "";
      return;
    }
    try {
      const fetchLogs = async (withRun: boolean) => {
        const suffix = withRun
          ? `?run_id=${encodeURIComponent(runId)}&limit=80`
          : `?limit=80`;
        const r = await fetch(
          `${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/browser/logs${suffix}`,
          { headers: authHeader(token()) },
        );
        const raw = await r.text();
        let data: { items?: BrowserLogItem[]; detail?: string };
        try {
          data = JSON.parse(raw) as { items?: BrowserLogItem[]; detail?: string };
        } catch {
          return { ok: false, items: [] as BrowserLogItem[], err: "parse error" };
        }
        if (!r.ok) {
          return { ok: false, items: [] as BrowserLogItem[], err: String(data.detail || raw || r.status) };
        }
        return { ok: true, items: data.items || [], err: "" };
      };
      let rs = await fetchLogs(true);
      if (runId && rs.ok && rs.items.length === 0) {
        rs = await fetchLogs(false);
      }
      if (!rs.ok) {
        if (st) st.textContent = rs.err;
        return;
      }
      renderBrowserLogsPanel(rs.items, runId);
    } catch (e) {
      if (st) st.textContent = String(e);
    }
  };

  /** Дублирует завершённые шаги оркестрации в основной чат как «групповой диалог» ролей. */
  const syncOrchestrationStepsToChat = (
    runId: string,
    data: {
      steps?: OrchStep[];
    },
  ) => {
    if (!currentProjectId) return;
    const steps = data.steps || [];
    const synced = readOrchChatSyncedKeys(currentProjectId, runId);
    let changed = false;
    for (const s of steps) {
      const sk = s.step_key || "";
      if (!sk || synced.has(sk)) continue;
      const st = s.status || "";
      const lbl = s.role_label || s.label || sk;
      const tsk = s.task_title || s.task;
      if (st === "done" && s.output && String(s.output).trim()) {
        projectAiChatHistory.push({
          role: "team",
          content: String(s.output).trim(),
          teamLabel: lbl,
          taskTitle: tsk,
          stepKey: sk,
        });
        synced.add(sk);
        changed = true;
      } else if (st === "error" && (s.error || s.output)) {
        const errBody = s.error || s.output || "";
        projectAiChatHistory.push({
          role: "team",
          content: `Ошибка шага: ${errBody}`,
          teamLabel: lbl,
          taskTitle: tsk,
          stepKey: sk,
        });
        synced.add(sk);
        changed = true;
      }
    }
    if (changed) {
      writeOrchChatSyncedKeys(currentProjectId, runId, synced);
      saveProjectAiChat();
      renderProjectAiChat();
    }
  };

  const renderOrchestrationPanel = (data: OrchRunData | null) => {
    const stepsEl = document.getElementById("orchestration-steps");
    const graphWrap = document.getElementById("orchestration-graph-wrap");
    const archWrap = document.getElementById("orchestration-arch-wrap");
    const execWrap = document.getElementById("orchestration-exec-wrap");
    const finalWrap = document.getElementById("orchestration-final-wrap");
    const statusEl = document.getElementById("orchestration-status-line");
    if (!stepsEl) return;
    if (archWrap) {
      archWrap.innerHTML = "";
    }
    if (execWrap) {
      execWrap.innerHTML = "";
    }
    if (finalWrap) {
      finalWrap.innerHTML = "";
    }
    if (graphWrap) {
      graphWrap.innerHTML = "";
    }
    if (!data) {
      stepsEl.innerHTML = `<p class="lp-muted">Выберите проект. Запуск — «Оркестрация ролей» (то же сообщение, что для чата).</p>`;
      if (statusEl) statusEl.textContent = "";
      renderOrchestrationStateLive(null);
      renderOrchestrationGraph(null);
      void loadBrowserLogs("");
      return;
    }
    renderOrchestrationStateLive(data);
    renderOrchestrationGraph(data);
    if (statusEl) {
      statusEl.textContent = `Прогон: ${data.status || "—"} · фаза ${data.phase || "—"} · run ${data.run_id || ""}${data.error && data.status === "error" ? ` · ${data.error}` : ""}`;
    }
    if (archWrap && data.architecture_md) {
      archWrap.innerHTML = `<div class="lp-orch-arch"><h3 style="margin:0 0 8px;font-size:15px;">Архитектура</h3><div class="lp-markdown lp-orch-pre" style="max-height:360px;overflow:auto;">${renderMarkdownHtml(String(data.architecture_md || ""))}</div></div>`;
    }
    const execTasks = data.execution_tasks;
    if (execWrap && execTasks && execTasks.length > 0) {
      const rows = execTasks
        .map((t) => {
          const title = String(t.title ?? "—");
          const kind = String(t.action_kind ?? "—");
          const st = String(t.status ?? "—");
          const err = t.error ? `<span class="lp-muted"> ${escapeHtml(String(t.error))}</span>` : "";
          return `<li><strong>${escapeHtml(title)}</strong> · <code>${escapeHtml(kind)}</code> · ${escapeHtml(st)}${err}</li>`;
        })
        .join("");
      execWrap.innerHTML = `<div class="lp-orch-arch" style="margin-top:10px;"><h3 style="margin:0 0 8px;font-size:15px;">Исполнение (воркеры / API)</h3><ul style="margin:0;padding-left:18px;font-size:13px;line-height:1.5;">${rows}</ul></div>`;
    }
    const steps = data.steps || [];
    let html = steps
      .map((s) => {
        const st = s.status || "pending";
        const lbl = s.role_label || s.label || s.step_key || "—";
        const tsk = s.task_title || s.task || "";
        const badge = orchBadgeClass(st);
        const body = s.error
          ? `<div class="lp-markdown lp-orch-pre lp-error">${renderMarkdownHtml(String(s.error || ""))}</div>`
          : s.output
            ? `<div class="lp-markdown lp-orch-pre">${renderMarkdownHtml(String(s.output || ""))}</div>`
            : `<p class="lp-muted" style="margin:0;font-size:12px;">${
                st === "running"
                  ? "Выполняется…"
                  : st === "skipped"
                    ? "Пропущено"
                    : "Ожидание"
              }</p>`;
        return `<article class="lp-orch-card"><div class="lp-orch-card-head"><span class="${badge}">${escapeHtml(lbl)}</span><span class="lp-muted" style="font-size:12px;">${escapeHtml(tsk)}</span></div>${body}</article>`;
      })
      .join("");
    stepsEl.innerHTML = html;
    const fin = data.final_report_md || data.final_message;
    if (finalWrap) {
      if (data.status === "paused") {
        finalWrap.innerHTML = `<div class="lp-orch-summary"><h3 style="margin:0 0 8px;font-size:14px;">Ожидание ответа</h3><p class="lp-muted" style="margin:0;font-size:13px;">Пайплайн приостановлен: проверьте шаги ниже — там могут быть вопросы к вам. Отправьте уточнение в чат и нажмите «Запустить ИИ команду» — цепочка продолжится с текущего шага.</p></div>`;
      } else if (fin && (data.status === "done" || data.status === "running")) {
        finalWrap.innerHTML = `<div class="lp-orch-summary"><h3 style="margin:0 0 8px;font-size:14px;">Итог для пользователя (PM)</h3><div class="lp-markdown lp-orch-pre">${renderMarkdownHtml(String(fin || ""))}</div></div>`;
      } else {
        finalWrap.innerHTML = "";
      }
    }
  };

  const applyOrchestrationRunRealtime = (runId: string, data: OrchRunData) => {
    renderOrchestrationPanel(data);
    syncOrchestrationStepsToChat(runId, data);
    if (orchModalStepKey) {
      const modalRoot = document.getElementById("orchestration-node-modal");
      if (modalRoot && !modalRoot.hasAttribute("hidden")) {
        if (!orchModalRunId || orchModalRunId === runId) {
          openOrchNodeModal(orchModalStepKey, data);
        }
      }
    }

    const prev = orchStreamLastRun;
    const iter = Number(data.iteration || 0);
    const batchStatus = String(data.batch_status || "—");
    const runLine = `run ${runId}: статус ${String(data.status || "—")} · фаза ${String(data.phase || "—")} · iteration ${iter} · batch ${batchStatus}`;
    if (!prev) {
      appendOrchestrationLiveLog(runLine);
    } else {
      if (
        (data.status || "") !== (prev.status || "") ||
        (data.phase || "") !== (prev.phase || "") ||
        Number(data.iteration || 0) !== Number(prev.iteration || 0) ||
        String(data.batch_status || "") !== String(prev.batch_status || "")
      ) {
        appendOrchestrationLiveLog(runLine);
      }
      const prevSteps = new Map((prev.steps || []).map((s) => [String(s.step_key || ""), s]));
      for (const s of data.steps || []) {
        const key = String(s.step_key || "");
        if (!key) continue;
        const p = prevSteps.get(key);
        const st = String(s.status || "");
        const pst = String(p?.status || "");
        if (!p || st !== pst) {
          appendOrchestrationLiveLog(`шаг ${key}: ${st}`);
        }
        const out = String(s.output || "");
        const pout = String(p?.output || "");
        if (out.length > pout.length && st === "running") {
          const delta = out.slice(pout.length).trim();
          if (delta) {
            const clip = delta.length > 220 ? `…${delta.slice(-220)}` : delta;
            appendOrchestrationLiveLog(`${key}: ${clip}`);
          }
        }
        if (s.error && s.error !== p?.error) {
          appendOrchestrationLiveLog(`шаг ${key}: ошибка ${String(s.error)}`);
        }
      }
      const prevExec = new Map((prev.execution_tasks || []).map((t) => [String(t.id || t.seq || ""), t]));
      for (const t of data.execution_tasks || []) {
        const id = String(t.id || t.seq || "");
        const p = prevExec.get(id);
        const st = String(t.status || "");
        const pst = String((p as Record<string, unknown> | undefined)?.status || "");
        if (!p || st !== pst) {
          appendOrchestrationLiveLog(`worker task ${String(t.title || id)}: ${st}`);
        }
      }
    }
    orchStreamLastRun = data;

    const now = Date.now();
    if (now - orchBrowserLogsTs > 5000) {
      orchBrowserLogsTs = now;
      void loadBrowserLogs(runId);
    }
  };

  const cloneRunData = (r: OrchRunData): OrchRunData => JSON.parse(JSON.stringify(r)) as OrchRunData;

  const runStateForDelta = (runId: string): OrchRunData => {
    if (orchStreamLastRun && orchStreamLastRun.run_id === runId) {
      return cloneRunData(orchStreamLastRun);
    }
    return {
      run_id: runId,
      status: "running",
      phase: "stream",
      iteration: 0,
      batch_status: "streaming",
      steps: [],
      execution_tasks: [],
    };
  };

  const applyOrchestrationDeltaEvent = (runId: string, ev: Record<string, unknown>) => {
    const typ = String(ev.type || "");
    if (typ === "run" && ev.run && typeof ev.run === "object") {
      applyOrchestrationRunRealtime(runId, ev.run as OrchRunData);
      return;
    }
    if (typ === "run_meta" && ev.patch && typeof ev.patch === "object") {
      const next = runStateForDelta(runId);
      const patch = ev.patch as Record<string, unknown>;
      for (const [k, v] of Object.entries(patch)) {
        (next as Record<string, unknown>)[k] = v;
      }
      applyOrchestrationRunRealtime(runId, next);
      return;
    }
    if (typ === "step_delta") {
      const stepKey = String(ev.step_key || "").trim();
      const patch = ev.patch as Record<string, unknown> | undefined;
      if (!stepKey || !patch || typeof patch !== "object") return;
      const next = runStateForDelta(runId);
      const steps = (next.steps || []) as OrchStep[];
      let s = steps.find((x) => String(x.step_key || "") === stepKey);
      if (!s) {
        s = { step_key: stepKey, status: "pending" };
        steps.push(s);
      }
      if (patch.output_full !== undefined) {
        s.output = String(patch.output_full ?? "");
      } else if (typeof patch.output_append === "string") {
        s.output = String(s.output || "") + patch.output_append;
      }
      for (const k of ["seq", "role_label", "task_title", "status", "error", "started_at", "finished_at", "structured"]) {
        if (Object.prototype.hasOwnProperty.call(patch, k)) {
          (s as Record<string, unknown>)[k] = patch[k];
        }
      }
      next.steps = [...steps].sort((a, b) => Number((a as Record<string, unknown>).seq || 999) - Number((b as Record<string, unknown>).seq || 999));
      applyOrchestrationRunRealtime(runId, next);
      return;
    }
    if (typ === "execution_task_delta") {
      const taskId = String(ev.task_id || "").trim();
      const patch = ev.patch as Record<string, unknown> | undefined;
      if (!taskId || !patch || typeof patch !== "object") return;
      const next = runStateForDelta(runId);
      const tasks = (next.execution_tasks || []) as Array<Record<string, unknown>>;
      let t = tasks.find((x) => String(x.id || x.seq || "") === taskId);
      if (!t) {
        t = { id: taskId, seq: 999, status: "pending" };
        tasks.push(t);
      }
      for (const [k, v] of Object.entries(patch)) {
        t[k] = v;
      }
      next.execution_tasks = [...tasks].sort((a, b) => Number(a.seq || 999) - Number(b.seq || 999));
      applyOrchestrationRunRealtime(runId, next);
      return;
    }
    if (typ === "stream_end") {
      appendOrchestrationLiveLog(`stream завершён: ${String(ev.status || "unknown")}`);
      return;
    }
    if (typ === "agent_trace") {
      const stepKey = String(ev.step_key || "agent");
      const message = String(ev.message || "").trim();
      if (message) appendOrchestrationLiveLog(`[trace:${stepKey}] ${message}`);
      return;
    }
  };

  const startOrchestrationStream = async (runId: string) => {
    if (!currentProjectId || !runId) return;
    stopOrchestrationStream();
    const ctrl = new AbortController();
    orchStreamAbort = ctrl;
    try {
      const resp = await fetch(
        `${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/agent-orchestrate/runs/${encodeURIComponent(runId)}/stream`,
        { headers: authHeader(token()), signal: ctrl.signal },
      );
      if (!resp.ok || !resp.body) {
        void pollOrchestration(runId);
        orchPollTimer = window.setInterval(() => void pollOrchestration(runId), 900);
        return;
      }
      const reader = resp.body.getReader();
      const dec = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += dec.decode(value, { stream: true });
        const parts = buffer.split("\n");
        buffer = parts.pop() ?? "";
        for (const raw of parts) {
          const line = raw.trim();
          if (!line) continue;
          let ev: Record<string, unknown>;
          try {
            ev = JSON.parse(line) as Record<string, unknown>;
          } catch {
            continue;
          }
          const typ = String(ev.type || "");
          if (typ === "error") {
            appendOrchestrationLiveLog(`stream error: ${String(ev.detail || "unknown")}`);
          } else {
            applyOrchestrationDeltaEvent(runId, ev);
          }
        }
      }
    } catch (e) {
      if (!ctrl.signal.aborted) {
        const msg = isFetchNetworkError(e) ? fetchNetworkHint("stream оркестрации") : String(e);
        appendOrchestrationLiveLog(`stream disconnected: ${msg}`);
        void pollOrchestration(runId);
        orchPollTimer = window.setInterval(() => void pollOrchestration(runId), 900);
      }
    } finally {
      if (orchStreamAbort === ctrl) {
        orchStreamAbort = null;
      }
    }
  };

  const pollOrchestration = async (runId: string) => {
    if (!currentProjectId) return;
    try {
      const r = await fetch(
        `${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/agent-orchestrate/runs/${encodeURIComponent(runId)}`,
        { headers: authHeader(token()) },
      );
      const raw = await r.text();
      let data: OrchRunData;
      try {
        data = JSON.parse(raw) as typeof data;
      } catch {
        return;
      }
      if (!r.ok) {
        if (r.status === 404 && currentProjectId) {
          sessionStorage.removeItem(`lambot_orch_run_${currentProjectId}`);
        }
        return;
      }
      applyOrchestrationRunRealtime(runId, data);
      const st = data.status;
      if (st === "done" || st === "error" || st === "paused") {
        stopOrchestrationPoll();
      }
    } catch {
      /* ignore */
    }
  };

  const renderDocsMarkdownPreview = () => {
    const preview = document.getElementById("docs-md-preview");
    if (!preview) return;
    const markdown = docsMdEditor ? getDocsMarkdownContent(docsMdEditor) : "";
    if (!markdown.trim()) {
      preview.innerHTML = `<p class="lp-muted">Предпросмотр появится здесь…</p>`;
      return;
    }
    preview.innerHTML = renderMarkdownHtml(markdown);
  };

  const ensureDocsMarkdownEditor = () => {
    if (docsMdEditor) return;
    const host = document.getElementById("docs-md-editor");
    if (!host) return;
    docsMdEditor = mountDocsMarkdownEditor(host, "");
    docsMdEditor.onDidChangeModelContent(() => renderDocsMarkdownPreview());
    renderDocsMarkdownPreview();
  };

  const resetDocsEditor = () => {
    currentDocId = "";
    const idEl = document.getElementById("docs-edit-id") as HTMLInputElement | null;
    const titleEl = document.getElementById("docs-edit-title") as HTMLInputElement | null;
    const typeEl = document.getElementById("docs-edit-type") as HTMLSelectElement | null;
    const slugEl = document.getElementById("docs-edit-slug") as HTMLInputElement | null;
    const chgEl = document.getElementById("docs-edit-change-summary") as HTMLInputElement | null;
    const versionsWrap = document.getElementById("docs-versions-wrap");
    if (idEl) idEl.value = "";
    if (titleEl) titleEl.value = "";
    if (typeEl) typeEl.value = "architecture";
    if (slugEl) slugEl.value = "";
    ensureDocsMarkdownEditor();
    if (docsMdEditor) setDocsMarkdownContent(docsMdEditor, "");
    renderDocsMarkdownPreview();
    if (chgEl) chgEl.value = "Создано/обновлено вручную";
    if (versionsWrap) versionsWrap.innerHTML = `<p class="lp-muted">Выберите документ в списке слева.</p>`;
  };

  const loadDocumentVersions = async (docId: string) => {
    const wrap = document.getElementById("docs-versions-wrap");
    if (!wrap || !currentProjectId || !docId) return;
    const resp = await fetch(
      `${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/documents/${encodeURIComponent(docId)}/versions`,
      { headers: authHeader(token()) },
    );
    if (!resp.ok) {
      wrap.innerHTML = `<p class="lp-muted">Ошибка загрузки версий: ${resp.status}</p>`;
      return;
    }
    const data = (await resp.json()) as { items?: Array<Record<string, unknown>> };
    const items = data.items || [];
    if (items.length === 0) {
      wrap.innerHTML = `<p class="lp-muted">Пока нет версий.</p>`;
      return;
    }
    wrap.innerHTML = items
      .map((v) => {
        const ver = Number(v.version ?? 0);
        const created = String(v.created_at ?? "");
        const summary = String(v.change_summary ?? "");
        return `<article class="lp-orch-card">
          <div class="lp-orch-card-head">
            <span class="lp-orch-badge ok">v${ver}</span>
            <span class="lp-muted" style="font-size:12px;">${escapeHtml(created)}</span>
          </div>
          <p class="lp-muted" style="margin:0 0 8px;font-size:12px;">${escapeHtml(summary || "—")}</p>
          <button type="button" class="lp-btn lp-btn-ghost lp-docs-load-ver" data-version="${ver}">Открыть</button>
        </article>`;
      })
      .join("");
    wrap.querySelectorAll(".lp-docs-load-ver").forEach((b) => {
      b.addEventListener("click", async () => {
        const ver = (b as HTMLButtonElement).dataset.version;
        if (!ver || !currentProjectId || !currentDocId) return;
        const r = await fetch(
          `${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/documents/${encodeURIComponent(currentDocId)}/versions/${encodeURIComponent(ver)}`,
          { headers: authHeader(token()) },
        );
        if (!r.ok) {
          alert(`Не удалось загрузить версию ${ver}: ${r.status}`);
          return;
        }
        const payload = (await r.json()) as { version?: Record<string, unknown> };
        const content = String(payload.version?.content_md ?? "");
        ensureDocsMarkdownEditor();
        if (docsMdEditor) setDocsMarkdownContent(docsMdEditor, content);
        renderDocsMarkdownPreview();
      });
    });
  };

  const loadDocumentById = async (docId: string) => {
    if (!currentProjectId || !docId) return;
    const resp = await fetch(
      `${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/documents/${encodeURIComponent(docId)}`,
      { headers: authHeader(token()) },
    );
    if (!resp.ok) return;
    const payload = (await resp.json()) as {
      document?: Record<string, unknown>;
      current_version?: Record<string, unknown> | null;
    };
    const doc = payload.document || {};
    const ver = payload.current_version || {};
    currentDocId = String(doc.id ?? "");
    const idEl = document.getElementById("docs-edit-id") as HTMLInputElement | null;
    const titleEl = document.getElementById("docs-edit-title") as HTMLInputElement | null;
    const typeEl = document.getElementById("docs-edit-type") as HTMLSelectElement | null;
    const slugEl = document.getElementById("docs-edit-slug") as HTMLInputElement | null;
    const chgEl = document.getElementById("docs-edit-change-summary") as HTMLInputElement | null;
    if (idEl) idEl.value = currentDocId;
    if (titleEl) titleEl.value = String(doc.title ?? "");
    if (typeEl) typeEl.value = String(doc.doc_type ?? "other");
    if (slugEl) slugEl.value = String(doc.slug ?? "");
    ensureDocsMarkdownEditor();
    if (docsMdEditor) setDocsMarkdownContent(docsMdEditor, String(ver.content_md ?? ""));
    renderDocsMarkdownPreview();
    if (chgEl) chgEl.value = "Обновлено вручную";
    await loadDocumentVersions(currentDocId);
  };

  const loadDocumentsList = async () => {
    const listEl = document.getElementById("docs-items-list");
    const stEl = document.getElementById("docs-status-line");
    const typeEl = document.getElementById("docs-filter-type") as HTMLSelectElement | null;
    if (!listEl || !stEl) return;
    if (!currentProjectId) {
      listEl.innerHTML = `<p class="lp-muted">Выберите проект</p>`;
      stEl.textContent = "";
      resetDocsEditor();
      return;
    }
    const params = new URLSearchParams();
    const tt = typeEl?.value?.trim() || "";
    if (tt) params.set("doc_type", tt);
    params.set("limit", "200");
    const r = await fetch(
      `${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/documents?${params.toString()}`,
      { headers: authHeader(token()) },
    );
    if (!r.ok) {
      listEl.innerHTML = `<p class="lp-muted">Ошибка загрузки: ${r.status}</p>`;
      stEl.textContent = "";
      return;
    }
    const data = (await r.json()) as { items?: ProjectDocItem[] };
    const items = data.items || [];
    stEl.textContent = `Документов: ${items.length}`;
    if (items.length === 0) {
      listEl.innerHTML = `<p class="lp-muted">Пока нет документов.</p>`;
      return;
    }
    listEl.innerHTML = items
      .map(
        (d) => `<article class="lp-orch-card">
          <div class="lp-orch-card-head">
            <span class="lp-orch-badge ok">${escapeHtml(d.doc_type)}</span>
            <span class="lp-muted" style="font-size:12px;">v${Number(d.latest_version || 0)}</span>
          </div>
          <h4 style="margin:0 0 4px;font-size:14px;">${escapeHtml(d.title || d.slug)}</h4>
          <p class="lp-muted" style="margin:0 0 8px;font-size:12px;">slug: ${escapeHtml(d.slug)} · ${escapeHtml(d.status)}</p>
          <div class="lp-row" style="gap:6px;">
            <button type="button" class="lp-btn lp-btn-ghost lp-doc-open" data-doc-id="${escapeHtml(d.id)}">Открыть</button>
            <button type="button" class="lp-btn lp-btn-ghost lp-doc-del" data-doc-id="${escapeHtml(d.id)}" style="color:#b91c1c;">Удалить</button>
          </div>
        </article>`,
      )
      .join("");
    listEl.querySelectorAll(".lp-doc-open").forEach((b) => {
      b.addEventListener("click", () => {
        const id = (b as HTMLButtonElement).dataset.docId;
        if (id) void loadDocumentById(id);
      });
    });
    listEl.querySelectorAll(".lp-doc-del").forEach((b) => {
      b.addEventListener("click", async () => {
        const id = (b as HTMLButtonElement).dataset.docId;
        if (!id || !currentProjectId) return;
        if (!window.confirm("Удалить документ?")) return;
        const rr = await fetch(
          `${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/documents/${encodeURIComponent(id)}`,
          { method: "DELETE", headers: authHeader(token()) },
        );
        if (!rr.ok) alert(`Ошибка удаления: ${rr.status}`);
        if (currentDocId === id) resetDocsEditor();
        void loadDocumentsList();
      });
    });
  };

  const saveDocumentFromEditor = async () => {
    if (!currentProjectId) {
      alert("Выберите проект");
      return;
    }
    const title = (document.getElementById("docs-edit-title") as HTMLInputElement | null)?.value.trim() || "";
    const docType = (document.getElementById("docs-edit-type") as HTMLSelectElement | null)?.value || "other";
    const slug = (document.getElementById("docs-edit-slug") as HTMLInputElement | null)?.value.trim() || "";
    ensureDocsMarkdownEditor();
    const content = docsMdEditor ? getDocsMarkdownContent(docsMdEditor) : "";
    const changeSummary =
      (document.getElementById("docs-edit-change-summary") as HTMLInputElement | null)?.value.trim() || "Updated";
    if (!title || !content.trim()) {
      alert("Нужны title и content");
      return;
    }
    if (!currentDocId) {
      const resp = await fetch(`${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/documents`, {
        method: "POST",
        headers: { ...authHeader(token()), "Content-Type": "application/json" },
        body: JSON.stringify({
          title,
          doc_type: docType,
          slug: slug || null,
          tags: [],
          content_md: content,
          change_summary: changeSummary,
          author_type: "user",
        }),
      });
      if (!resp.ok) {
        const txt = await resp.text();
        alert(txt || `Ошибка ${resp.status}`);
        return;
      }
      const payload = (await resp.json()) as { document?: { id?: string } };
      currentDocId = String(payload.document?.id || "");
      await loadDocumentsList();
      if (currentDocId) await loadDocumentById(currentDocId);
      return;
    }
    const vr = await fetch(
      `${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/documents/${encodeURIComponent(currentDocId)}/versions`,
      {
        method: "POST",
        headers: { ...authHeader(token()), "Content-Type": "application/json" },
        body: JSON.stringify({
          content_md: content,
          change_summary: changeSummary,
          author_type: "user",
        }),
      },
    );
    if (!vr.ok) {
      const txt = await vr.text();
      alert(txt || `Ошибка ${vr.status}`);
      return;
    }
    await fetch(`${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/documents/${encodeURIComponent(currentDocId)}`, {
      method: "PATCH",
      headers: { ...authHeader(token()), "Content-Type": "application/json" },
      body: JSON.stringify({ title, doc_type: docType, slug: slug || null, status: "active" }),
    });
    await loadDocumentsList();
    await loadDocumentVersions(currentDocId);
  };

  const searchDocuments = async () => {
    const wrap = document.getElementById("docs-search-results");
    const q = (document.getElementById("docs-search-query") as HTMLInputElement | null)?.value.trim() || "";
    if (!wrap) return;
    if (!currentProjectId) {
      wrap.innerHTML = `<p class="lp-muted">Выберите проект</p>`;
      return;
    }
    if (!q) {
      wrap.innerHTML = `<p class="lp-muted">Введите запрос</p>`;
      return;
    }
    const r = await fetch(`${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/documents/search`, {
      method: "POST",
      headers: { ...authHeader(token()), "Content-Type": "application/json" },
      body: JSON.stringify({ query: q, top_k: 8 }),
    });
    if (!r.ok) {
      wrap.innerHTML = `<p class="lp-muted">Ошибка поиска: ${r.status}</p>`;
      return;
    }
    const data = (await r.json()) as { items?: Array<Record<string, unknown>> };
    const items = data.items || [];
    if (items.length === 0) {
      wrap.innerHTML = `<p class="lp-muted">Ничего не найдено.</p>`;
      return;
    }
    wrap.innerHTML = items
      .map((it) => {
        const score = Number(it.score ?? 0).toFixed(3);
        const docId = String(it.document_id ?? "");
        return `<article class="lp-orch-card">
          <div class="lp-orch-card-head">
            <span class="lp-orch-badge run">${escapeHtml(String(it.doc_type ?? "doc"))}</span>
            <span class="lp-muted" style="font-size:12px;">score ${escapeHtml(score)}</span>
          </div>
          <h4 style="margin:0 0 6px;font-size:14px;">${escapeHtml(String(it.title ?? ""))}</h4>
          <p class="lp-muted" style="margin:0 0 8px;font-size:12px;">${escapeHtml(String(it.excerpt ?? ""))}</p>
          <button type="button" class="lp-btn lp-btn-ghost lp-doc-search-open" data-doc-id="${escapeHtml(docId)}">Открыть документ</button>
        </article>`;
      })
      .join("");
    wrap.querySelectorAll(".lp-doc-search-open").forEach((b) => {
      b.addEventListener("click", () => {
        const id = (b as HTMLButtonElement).dataset.docId;
        if (id) void loadDocumentById(id);
      });
    });
  };

  const closeTrackerTaskModal = () => {
    const root = document.getElementById("tracker-task-modal");
    if (!root) return;
    root.setAttribute("hidden", "");
    document.body.classList.remove("lp-modal-open");
  };

  const openTrackerTaskModal = async (itemId: string) => {
    if (!currentProjectId || !itemId) return;
    const root = document.getElementById("tracker-task-modal");
    const body = document.getElementById("tracker-task-modal-body");
    if (!root || !body) return;
    body.innerHTML = `<p class="lp-muted">Загрузка…</p>`;
    root.removeAttribute("hidden");
    document.body.classList.add("lp-modal-open");
    try {
      const [itemResp, eventsResp] = await Promise.all([
        fetch(`${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/tracker/items/${encodeURIComponent(itemId)}`, {
          headers: authHeader(token()),
        }),
        fetch(
          `${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/tracker/items/${encodeURIComponent(itemId)}/events?limit=60`,
          { headers: authHeader(token()) },
        ),
      ]);
      const itemRaw = await itemResp.text();
      const eventsRaw = await eventsResp.text();
      let item: Record<string, unknown> = {};
      let evData: { events?: Array<Record<string, unknown>> } = {};
      try {
        item = JSON.parse(itemRaw) as Record<string, unknown>;
      } catch {
        item = {};
      }
      try {
        evData = JSON.parse(eventsRaw) as { events?: Array<Record<string, unknown>> };
      } catch {
        evData = {};
      }
      if (!itemResp.ok) {
        body.innerHTML = `<p class="lp-error">Ошибка загрузки задачи: ${escapeHtml(String((item as { detail?: string }).detail || itemRaw || itemResp.status))}</p>`;
        return;
      }
      const meta = (item.meta && typeof item.meta === "object" ? (item.meta as Record<string, unknown>) : {}) || {};
      const resultObj = (meta.last_result && typeof meta.last_result === "object"
        ? (meta.last_result as Record<string, unknown>)
        : {}) as Record<string, unknown>;
      const lastErr = String(meta.last_error || "");
      const commentary = String(resultObj.commentary || resultObj.output || "").trim();
      const workDone = Array.isArray(resultObj.work_done)
        ? resultObj.work_done.map((x) => String(x || "").trim()).filter(Boolean)
        : [];
      const dodObj = resultObj.dod && typeof resultObj.dod === "object"
        ? (resultObj.dod as Record<string, unknown>)
        : null;
      const dodDone = Boolean(dodObj?.done);
      const dodStatus = String(dodObj?.status || "").trim();
      const dodNotes = String(dodObj?.notes || "").trim();
      const dodCriteria = Array.isArray(dodObj?.criteria)
        ? (dodObj?.criteria as unknown[]).map((x) => String(x || "").trim()).filter(Boolean)
        : [];
      const resultJson = resultObj && Object.keys(resultObj).length > 0 ? JSON.stringify(resultObj, null, 2) : "";
      const events = Array.isArray(evData.events) ? evData.events : [];
      const eventsHtml = events.length
        ? events
            .map((e) => {
              const at = String(e.created_at || "");
              const action = String(e.action || "");
              const actor = String(e.actor_ref || "");
              const payload =
                e.payload && typeof e.payload === "object"
                  ? JSON.stringify(e.payload as Record<string, unknown>, null, 2)
                  : "";
              return `<article class="lp-orch-card" style="margin-bottom:8px;">
                <div class="lp-orch-card-head">
                  <span class="lp-orch-badge run">${escapeHtml(action || "event")}</span>
                  <span class="lp-muted" style="font-size:12px;">${escapeHtml(at ? new Date(at).toLocaleString() : "—")}</span>
                </div>
                <p class="lp-muted" style="margin:6px 0 0;font-size:12px;">actor: ${escapeHtml(actor || "—")}</p>
                ${payload ? `<pre class="lp-orch-pre" style="margin-top:8px;">${escapeHtml(payload)}</pre>` : ""}
              </article>`;
            })
            .join("")
        : `<p class="lp-muted">Событий пока нет.</p>`;
      body.innerHTML = `
        <div class="lp-orch-grid">
          <article class="lp-orch-card">
            <div class="lp-orch-card-head">
              <span class="lp-orch-badge wait">${escapeHtml(String(item.kind || "task"))}</span>
              <span class="lp-orch-badge run">${escapeHtml(String(item.status || "unknown"))}</span>
            </div>
            <p style="margin:8px 0;font-weight:600;">${escapeHtml(String(item.title || "—"))}</p>
            <p class="lp-muted" style="font-size:12px;white-space:pre-wrap;">${escapeHtml(String(item.description || ""))}</p>
            <p class="lp-muted" style="font-size:12px;">execution_id: <code>${escapeHtml(String(item.last_execution_id || "—"))}</code></p>
          </article>
          <article class="lp-orch-card">
            <h4 style="margin:0 0 8px;font-size:14px;">Результат агента</h4>
            ${
              commentary
                ? `<p style="margin:0 0 10px;font-size:13px;white-space:pre-wrap;">${escapeHtml(commentary)}</p>`
                : `<p class="lp-muted" style="margin:0 0 10px;">Комментарий агента пока отсутствует.</p>`
            }
            <h4 style="margin:0 0 8px;font-size:14px;">Что было сделано</h4>
            ${
              workDone.length
                ? `<ul style="margin:0 0 10px 18px;padding:0;">${workDone
                    .map((row) => `<li style="margin:0 0 4px;">${escapeHtml(row)}</li>`)
                    .join("")}</ul>`
                : `<p class="lp-muted" style="margin:0 0 10px;">Нет явного списка выполненных действий.</p>`
            }
            <h4 style="margin:0 0 8px;font-size:14px;">DoD</h4>
            <p style="margin:0 0 8px;font-size:13px;">
              <span class="lp-orch-badge ${dodDone ? "ok" : "wait"}">${escapeHtml(dodDone ? "DONE" : "NOT DONE")}</span>
              <span class="lp-muted" style="margin-left:8px;">${escapeHtml(dodStatus || "unknown")}</span>
            </p>
            ${
              dodCriteria.length
                ? `<ul style="margin:0 0 10px 18px;padding:0;">${dodCriteria
                    .map((row) => `<li style="margin:0 0 4px;">${escapeHtml(row)}</li>`)
                    .join("")}</ul>`
                : ""
            }
            ${dodNotes ? `<p class="lp-muted" style="margin:0 0 10px;">${escapeHtml(dodNotes)}</p>` : ""}
            <details>
              <summary class="lp-muted" style="cursor:pointer;">Показать raw result JSON</summary>
              ${resultJson ? `<pre class="lp-orch-pre" style="margin-top:8px;">${escapeHtml(resultJson)}</pre>` : `<p class="lp-muted">Результат ещё не сформирован.</p>`}
            </details>
            ${lastErr ? `<h4 style="margin:12px 0 8px;font-size:14px;">Ошибка</h4><pre class="lp-orch-pre lp-error">${escapeHtml(lastErr)}</pre>` : ""}
          </article>
        </div>
        <h4 style="margin:12px 0 8px;font-size:14px;">История выполнения</h4>
        <div>${eventsHtml}</div>
      `;
    } catch (e) {
      body.innerHTML = `<p class="lp-error">${escapeHtml(String(e))}</p>`;
    }
  };

  const loadTrackerItems = async () => {
    const listEl = document.getElementById("tracker-items-list");
    const stEl = document.getElementById("tracker-status-line");
    if (!listEl) return;
    if (!currentProjectId) {
      listEl.innerHTML = `<p class="lp-muted">Выберите проект.</p>`;
      if (stEl) stEl.textContent = "";
      return;
    }
    if (stEl) stEl.textContent = "Загрузка…";
    try {
      const r = await fetch(`${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/tracker/items`, {
        headers: authHeader(token()),
      });
      const raw = await r.text();
      let data: { items?: Array<Record<string, unknown>>; detail?: string };
      try {
        data = JSON.parse(raw) as { items?: Array<Record<string, unknown>> };
      } catch {
        listEl.innerHTML = `<p class="lp-muted">Ошибка разбора ответа</p>`;
        if (stEl) stEl.textContent = "";
        return;
      }
      if (!r.ok) {
        listEl.innerHTML = `<p class="lp-muted">${escapeHtml(String(data.detail || raw))}</p>`;
        if (stEl) stEl.textContent = "";
        return;
      }
      const items = data.items || [];
      if (stEl) stEl.textContent = `${items.length} карточек`;
      if (items.length === 0) {
        listEl.innerHTML = `<p class="lp-muted">Пока нет задач — создайте выше.</p>`;
        return;
      }
      const canDispatch = (it: Record<string, unknown>) => {
        const k = String(it.kind || "");
        const st = String(it.status || "");
        if (k !== "task") return false;
        return ["backlog", "ready", "failed", "blocked"].includes(st);
      };
      const canClose = (it: Record<string, unknown>) => {
        const st = String(it.status || "");
        return !["cancelled", "done"].includes(st);
      };
      const canEdit = (it: Record<string, unknown>) => {
        const st = String(it.status || "");
        return ["backlog", "ready", "blocked", "failed"].includes(st);
      };
      const statusBadgeClass = (raw: string) => {
        if (raw === "done") return "lp-orch-badge ok";
        if (raw === "cancelled") return "lp-orch-badge skip";
        if (raw === "failed") return "lp-orch-badge err";
        if (raw === "in_progress" || raw === "queued") return "lp-orch-badge run";
        return "lp-orch-badge wait";
      };
      listEl.innerHTML = items
        .map((it) => {
          const id = String(it.id || "");
          const rawSt = String(it.status || "");
          const title = escapeHtml(String(it.title || "—"));
          const kind = escapeHtml(String(it.kind || ""));
          const st = escapeHtml(rawSt);
          const role = it.assignee_role ? escapeHtml(String(it.assignee_role)) : "—";
          const key = it.assignee_agent_key ? escapeHtml(String(it.assignee_agent_key)) : "";
          const editBtn = canEdit(it)
            ? `<button type="button" class="lp-btn lp-btn-ghost lp-tracker-edit" data-id="${escapeHtml(id)}">Редактировать</button>`
            : "";
          const dispatchBtn = canDispatch(it)
            ? `<button type="button" class="lp-btn lp-btn-ghost lp-tracker-dispatch" data-id="${escapeHtml(id)}">В очередь</button>`
            : "";
          const closeBtn = canClose(it)
            ? `<button type="button" class="lp-btn lp-btn-ghost lp-tracker-close" data-id="${escapeHtml(id)}">Закрыть</button>`
            : "";
          const detailsBtn = `<button type="button" class="lp-btn lp-btn-ghost lp-tracker-result" data-id="${escapeHtml(id)}">Результат</button>`;
          const actions =
            editBtn || dispatchBtn || closeBtn || detailsBtn
              ? `<div class="lp-row" style="gap:6px;margin-top:8px;flex-wrap:wrap;">${detailsBtn}${editBtn}${dispatchBtn}${closeBtn}</div>`
              : "";
          return `<article class="lp-orch-card"><div class="lp-orch-card-head"><span class="lp-orch-badge wait">${kind}</span><span class="${statusBadgeClass(rawSt)}" style="font-size:12px;">${st}</span></div><p style="margin:6px 0;font-weight:600;">${title}</p><p class="lp-muted" style="font-size:12px;margin:0;">Роль: ${role}</p><p class="lp-muted" style="font-size:11px;margin:4px 0 0;word-break:break-all;">${key}</p>${actions}</article>`;
        })
        .join("");
      const showTrackerEdit = (show: boolean) => {
        const w = document.getElementById("tracker-edit-wrap");
        if (w) w.style.display = show ? "block" : "none";
      };
      listEl.querySelectorAll(".lp-tracker-edit").forEach((b) => {
        b.addEventListener("click", async () => {
          const id = (b as HTMLButtonElement).dataset.id;
          if (!id || !currentProjectId) return;
          try {
            const resp = await fetch(
              `${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/tracker/items/${encodeURIComponent(id)}`,
              { headers: authHeader(token()) },
            );
            const raw = await resp.text();
            let item: Record<string, unknown>;
            try {
              item = JSON.parse(raw) as Record<string, unknown>;
            } catch {
              alert("Некорректный ответ");
              return;
            }
            if (!resp.ok) {
              alert(typeof item.detail === "string" ? item.detail : raw || String(resp.status));
              return;
            }
            (document.getElementById("tracker-edit-id") as HTMLInputElement).value = id;
            const kind = String(item.kind || "task");
            (document.getElementById("tracker-edit-kind") as HTMLInputElement).value = kind;
            (document.getElementById("tracker-edit-title") as HTMLInputElement).value = String(item.title ?? "");
            (document.getElementById("tracker-edit-desc") as HTMLTextAreaElement).value = String(item.description ?? "");
            const roleSel = document.getElementById("tracker-edit-role") as HTMLSelectElement;
            const roleRow = document.getElementById("tracker-edit-role-row");
            if (kind === "epic") {
              if (roleRow) roleRow.style.display = "none";
            } else {
              if (roleRow) roleRow.style.display = "";
              const r = item.assignee_role ? String(item.assignee_role) : "";
              roleSel.value = r;
            }
            showTrackerEdit(true);
            document.getElementById("tracker-edit-wrap")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
          } catch (e) {
            alert(String(e));
          }
        });
      });
      listEl.querySelectorAll(".lp-tracker-dispatch").forEach((b) => {
        b.addEventListener("click", async () => {
          const id = (b as HTMLButtonElement).dataset.id;
          if (!id || !currentProjectId) return;
          (b as HTMLButtonElement).disabled = true;
          try {
            const resp = await fetch(
              `${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/tracker/items/${encodeURIComponent(id)}/dispatch`,
              { method: "POST", headers: authHeader(token()) },
            );
            const txt = await resp.text();
            if (!resp.ok) {
              alert(txt || String(resp.status));
            }
          } catch (e) {
            alert(String(e));
          }
          void loadTrackerItems();
        });
      });
      listEl.querySelectorAll(".lp-tracker-close").forEach((b) => {
        b.addEventListener("click", async () => {
          const id = (b as HTMLButtonElement).dataset.id;
          if (!id || !currentProjectId) return;
          if (!window.confirm("Закрыть эту карточку? Статус станет «cancelled».")) return;
          (b as HTMLButtonElement).disabled = true;
          try {
            const resp = await fetch(
              `${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/tracker/items/${encodeURIComponent(id)}`,
              {
                method: "PATCH",
                headers: { ...authHeader(token()), "Content-Type": "application/json" },
                body: JSON.stringify({ status: "cancelled" }),
              },
            );
            const txt = await resp.text();
            if (!resp.ok) {
              alert(txt || String(resp.status));
            }
          } catch (e) {
            alert(String(e));
          }
          void loadTrackerItems();
        });
      });
      listEl.querySelectorAll(".lp-tracker-result").forEach((b) => {
        b.addEventListener("click", () => {
          const id = (b as HTMLButtonElement).dataset.id;
          if (id) void openTrackerTaskModal(id);
        });
      });
    } catch (e) {
      listEl.innerHTML = `<p class="lp-muted">${escapeHtml(String(e))}</p>`;
      if (stEl) stEl.textContent = "";
    }
  };

  const restoreOrchestrationForProject = () => {
    closeOrchNodeModal();
    closeTrackerTaskModal();
    stopOrchestrationPoll();
    stopOrchestrationStream();
    resetOrchestrationLiveLog();
    void loadTrackerItems();
    void loadDocumentsList();
    if (!currentProjectId) {
      renderOrchestrationPanel(null);
      void loadBrowserLogs("");
      resetDocsEditor();
      return;
    }
    const last = sessionStorage.getItem(`lambot_orch_run_${currentProjectId}`);
    if (last) {
      appendOrchestrationLiveLog(`подключение к stream run ${last}…`);
      void startOrchestrationStream(last);
      return;
    }
    void (async () => {
      try {
        const r = await fetch(`${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/ai-team/state`, {
          headers: authHeader(token()),
        });
        const j = (await r.json()) as { run?: Record<string, unknown> };
        if (r.ok && j.run) {
          renderOrchestrationPanel(j.run);
          const rid = typeof j.run.run_id === "string" ? j.run.run_id : "";
          if (rid) {
            void loadBrowserLogs(rid);
            const um = typeof j.run.user_message === "string" ? j.run.user_message.trim() : "";
            if (um && projectAiChatHistory.length === 0) {
              projectAiChatHistory.push({ role: "user", content: um });
              saveProjectAiChat();
              renderProjectAiChat();
            }
            syncOrchestrationStepsToChat(rid, j.run as { steps?: OrchStep[] });
            const st = typeof j.run.status === "string" ? j.run.status : "";
            if (st === "running") {
              sessionStorage.setItem(`lambot_orch_run_${currentProjectId}`, rid);
              appendOrchestrationLiveLog(`подключение к stream run ${rid}…`);
              void startOrchestrationStream(rid);
            }
          }
        } else {
          renderOrchestrationPanel(null);
        }
      } catch {
        renderOrchestrationPanel(null);
      }
    })();
  };

  const startOrchestration = async () => {
    if (!currentProjectId) {
      alert("Выберите проект");
      return;
    }
    const orchInp = document.getElementById("orchestration-user-message") as HTMLTextAreaElement | null;
    const chatInp = document.getElementById("project-ai-chat-input") as HTMLTextAreaElement | null;
    const msg = (orchInp?.value?.trim() || chatInp?.value?.trim() || "").trim();
    if (!msg) {
      alert("Введите запрос в поле оркестрации или сообщение в чате по проекту");
      return;
    }
    stopOrchestrationPoll();
    stopOrchestrationStream();
    const stLine = document.getElementById("orchestration-status-line");
    if (stLine) stLine.textContent = "Подготовка запуска…";
    try {
      let endpoint = `${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/agent-orchestrate/start`;
      let continueRunId = "";
      const lastRunId = sessionStorage.getItem(`lambot_orch_run_${currentProjectId}`) || "";
      if (lastRunId) {
        try {
          const rr = await fetch(
            `${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/agent-orchestrate/runs/${encodeURIComponent(lastRunId)}`,
            { headers: authHeader(token()) },
          );
          if (rr.status === 404) {
            sessionStorage.removeItem(`lambot_orch_run_${currentProjectId}`);
          } else if (rr.ok) {
            const rd = (await rr.json()) as { status?: string };
            const st = String(rd.status || "").toLowerCase();
            if (st === "paused" || st === "done" || st === "error") {
              continueRunId = lastRunId;
              endpoint = `${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/agent-orchestrate/runs/${encodeURIComponent(lastRunId)}/continue`;
              if (stLine) stLine.textContent = st === "paused" ? "Продолжение приостановленного запуска…" : "Продолжение run по новому сообщению…";
              appendOrchestrationLiveLog(
                `${st === "paused" ? "resume" : "continue"} run ${lastRunId}: отправлено сообщение пользователя`,
              );
            } else if (st === "running") {
              if (stLine) stLine.textContent = `run ${lastRunId} уже выполняется — дождитесь завершения шага`;
              appendOrchestrationLiveLog(`run ${lastRunId} уже running: новое сообщение будет доступно после завершения текущего цикла`);
              void startOrchestrationStream(lastRunId);
              return;
            }
          }
        } catch {
          /* ignore and start fresh */
        }
      }
      if (!continueRunId) {
        resetOrchestrationLiveLog();
      }

      const resp = await fetch(endpoint, {
        method: "POST",
        headers: { ...authHeader(token()), "Content-Type": "application/json" },
        body: JSON.stringify({ message: msg, history: [] }),
      });
      const raw = await resp.text();
      let payload: { run_id?: string; detail?: string };
      try {
        payload = JSON.parse(raw) as { run_id?: string; detail?: string };
      } catch {
        alert(resp.ok ? "Некорректный ответ" : `Ошибка ${resp.status}`);
        if (stLine) stLine.textContent = "";
        return;
      }
      if (!resp.ok || !payload.run_id) {
        alert(payload.detail || raw || String(resp.status));
        if (stLine) stLine.textContent = "";
        return;
      }
      projectAiChatHistory.push({ role: "user", content: msg });
      saveProjectAiChat();
      renderProjectAiChat();
      if (orchInp) orchInp.value = "";
      if (chatInp) chatInp.value = "";
      const effectiveRunId = continueRunId || payload.run_id;
      sessionStorage.setItem(`lambot_orch_run_${currentProjectId}`, effectiveRunId);
      appendOrchestrationLiveLog(`stream run ${effectiveRunId}: подключение…`);
      void startOrchestrationStream(effectiveRunId);
    } catch (e) {
      const msg = isFetchNetworkError(e) ? fetchNetworkHint("запуск оркестрации") : String(e);
      alert(msg);
      write({ error: msg, raw_error: String(e) });
      if (stLine) stLine.textContent = "";
    }
  };

  const refreshSession = async () => {
    const el = document.getElementById("portal-session");
    const t = token();
    if (!t) {
      if (el) el.textContent = "Не авторизован — откройте #/login";
      return;
    }
    const r = await fetch(`${GATEWAY_BASE}/api/v1/auth/me`, { headers: authHeader(t) });
    if (!r.ok) {
      if (el) el.textContent = "JWT недействителен — войдите снова";
      return;
    }
    const me = await r.json();
    if (el) el.textContent = `${(me as { email?: string }).email || "user"} · tenant ${(me as { tenant_id?: string }).tenant_id || "—"}`;
  };

  const agentStatusRu = (s: string) => {
    const m: Record<string, string> = {
      ok: "Онлайн",
      stale: "Задержка",
      offline: "Нет связи",
      no_data: "Нет метрик",
    };
    return m[s] || s;
  };

  const provisionInstallRu = (ok: boolean | null | undefined) => {
    if (ok === null || ok === undefined) return "—";
    return ok ? "Успех" : "Ошибка";
  };

  const loadActivityLogs = async () => {
    const tbody = document.getElementById("activity-logs-body");
    const hint = document.getElementById("activity-logs-hint");
    const det = document.getElementById("activity-logs-detail");
    if (!currentProjectId) {
      lastActivityItems = [];
      if (tbody) tbody.innerHTML = `<tr><td colspan="5" class="lp-muted">Выберите проект</td></tr>`;
      if (hint) hint.textContent = "";
      if (det) det.textContent = "";
      return;
    }
    const r = await fetch(`${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/activity-logs?limit=40`, {
      headers: authHeader(token()),
    });
    const raw = (await r.json()) as {
      items?: unknown[];
      clickhouse?: { enabled?: boolean; reachable?: boolean };
    };
    if (hint) {
      const ch = raw.clickhouse;
      hint.textContent = ch
        ? `ClickHouse: ${ch.enabled ? (ch.reachable ? "подключён" : "нет связи") : "не задан (CLICKHOUSE_HOST)"}`
        : "";
    }
    if (!tbody) return;
    if (!r.ok) {
      tbody.innerHTML = `<tr><td colspan="5" class="lp-muted">Журнал: ошибка ${r.status}</td></tr>`;
      return;
    }
    const items = (raw.items || []) as Array<{
      ts?: string;
      action?: string;
      status?: string;
      summary?: string;
      actor?: string | null;
    }>;
    lastActivityItems = items as Array<Record<string, unknown>>;
    if (items.length === 0) {
      tbody.innerHTML = `<tr><td colspan="5" class="lp-muted">Пока нет записей</td></tr>`;
      return;
    }
    tbody.innerHTML = items
      .map(
        (it, i) => `<tr data-act-idx="${i}" style="cursor:pointer;">
      <td style="padding:6px;border-bottom:1px solid #f1f5f9;font-size:11px;">${escapeHtml(it.ts || "—")}</td>
      <td style="padding:6px;border-bottom:1px solid #f1f5f9;">${escapeHtml(it.action || "")}</td>
      <td style="padding:6px;border-bottom:1px solid #f1f5f9;">${escapeHtml(it.status || "")}</td>
      <td style="padding:6px;border-bottom:1px solid #f1f5f9;max-width:260px;word-break:break-word;">${escapeHtml((it.summary || "").slice(0, 180))}</td>
      <td style="padding:6px;border-bottom:1px solid #f1f5f9;font-size:11px;">${escapeHtml(it.actor || "—")}</td>
    </tr>`,
      )
      .join("");
  };

  const refreshInfraUi = async () => {
    const resBody = document.getElementById("infra-resources-body");
    const gl = document.getElementById("infra-gitlab-target") as HTMLSelectElement | null;
    const nx = document.getElementById("infra-nexus-target") as HTMLSelectElement | null;
    const tplTarget = document.getElementById("infra-template-target") as HTMLSelectElement | null;
    if (!currentProjectId) {
      if (resBody) resBody.innerHTML = `<tr><td colspan="7" class="lp-muted">Выберите проект</td></tr>`;
      if (gl) gl.innerHTML = `<option value="">— не назначен —</option>`;
      if (nx) nx.innerHTML = `<option value="">— не назначен —</option>`;
      if (tplTarget) tplTarget.innerHTML = `<option value="">— выберите SSH-сервер —</option>`;
      infraTemplateCatalogProjectId = "";
      void loadActivityLogs();
      return;
    }
    const t = token();
    const h = authHeader(t);
    const [tRes, aRes, resRes] = await Promise.all([
      fetch(`${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/targets`, { headers: h }),
      fetch(`${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/infra/service-assignments`, { headers: h }),
      fetch(`${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/infra/resources`, { headers: h }),
    ]);
    type Tgt = { id: string; host: string; label: string; target_kind?: string };
    const targets: Tgt[] = tRes.ok ? ((await tRes.json()) as Tgt[]) : [];
    const sshTargets = targets.filter((x) => x.target_kind !== "kubernetes");
    const optHtml = `<option value="">— не назначен —</option>${sshTargets
      .map((x) => `<option value="${escapeHtml(x.id)}">${escapeHtml(x.host)} (${escapeHtml(x.label)})</option>`)
      .join("")}`;
    if (gl) gl.innerHTML = optHtml;
    if (nx) nx.innerHTML = optHtml;
    if (tplTarget) tplTarget.innerHTML = `<option value="">— выберите SSH-сервер —</option>${sshTargets
      .map((x) => `<option value="${escapeHtml(x.id)}">${escapeHtml(x.host)} (${escapeHtml(x.label)})</option>`)
      .join("")}`;
    if (infraTemplateCatalogProjectId !== currentProjectId) {
      infraTemplateCatalogProjectId = currentProjectId;
      void loadInfraTemplateCatalog();
    }
    let gitlabT = "";
    let nexusT = "";
    if (aRes.ok) {
      const assigns = (await aRes.json()) as Array<{ service_role: string; target_id: string }>;
      for (const a of assigns) {
        if (a.service_role === "gitlab") gitlabT = a.target_id;
        if (a.service_role === "nexus") nexusT = a.target_id;
      }
    }
    if (gl && gitlabT) gl.value = gitlabT;
    if (nx && nexusT) nx.value = nexusT;
    if (resBody) {
      if (!resRes.ok) {
        resBody.innerHTML = `<tr><td colspan="7" class="lp-muted">resources: ${resRes.status}</td></tr>`;
      } else {
        const resources = (await resRes.json()) as Array<{
          id: string;
          category: string;
          label: string;
          value_json: Record<string, unknown>;
          has_secret: boolean;
          enabled?: boolean;
        }>;
        const hostOf = (r: (typeof resources)[0]) => {
          const v = r.value_json || {};
          if (r.category === "service") return String(v.host ?? v.fqdn ?? "—");
          if (r.category === "gitlab") return String(v.gitlab_url ?? v.target_host ?? "—");
          if (r.category === "nexus")
            return String(v.nexus_url ?? v.nexus_host ?? v.target_host ?? v.host ?? "—");
          return String(v.host ?? v.url ?? v.fqdn ?? "—");
        };
        const userOf = (r: (typeof resources)[0]) => {
          const v = r.value_json || {};
          if (r.category === "service") return String(v.user ?? "—");
          if (r.category === "gitlab") return String(v.username ?? "—");
          if (r.category === "nexus") return String(v.admin_username ?? v.username ?? "—");
          return "—";
        };
        const sourceOf = (r: (typeof resources)[0]) => {
          if (r.category === "service") return "Вручную";
          if (r.category === "gitlab") return "Ansible → GitLab";
          if (r.category === "nexus") return "Ansible → Nexus";
          return r.category;
        };
        if (resources.length === 0) {
          resBody.innerHTML = `<tr><td colspan="7" class="lp-muted">Пока нет записей — добавьте сервис или дождитесь Ansible</td></tr>`;
        } else {
          resBody.innerHTML = resources
            .map((r) => {
              const en = r.enabled !== false;
              const canEdit = r.category === "service";
              const secretCell = r.has_secret ? "••••" : "—";
              const editBtn = canEdit
                ? `<button type="button" class="lp-btn lp-btn-ghost" style="padding:4px 8px;font-size:12px" data-infra-edit="${escapeHtml(r.id)}">Изменить</button>`
                : "";
              return `<tr data-infra-row="${escapeHtml(r.id)}">
            <td style="padding:6px;border-bottom:1px solid #f1f5f9;">${escapeHtml(r.label)}</td>
            <td style="padding:6px;border-bottom:1px solid #f1f5f9;max-width:200px;word-break:break-all;font-size:12px;">${escapeHtml(hostOf(r))}</td>
            <td style="padding:6px;border-bottom:1px solid #f1f5f9;font-size:12px;">${escapeHtml(userOf(r))}</td>
            <td style="padding:6px;border-bottom:1px solid #f1f5f9;">${secretCell}</td>
            <td style="padding:6px;border-bottom:1px solid #f1f5f9;"><input type="checkbox" data-infra-enabled="${escapeHtml(r.id)}" ${en ? "checked" : ""} aria-label="Включён" /></td>
            <td style="padding:6px;border-bottom:1px solid #f1f5f9;font-size:12px;">${escapeHtml(sourceOf(r))}</td>
            <td style="padding:6px;border-bottom:1px solid #f1f5f9;white-space:nowrap;">${editBtn}
              <button type="button" class="lp-btn lp-btn-ghost" style="padding:4px 8px;font-size:12px;color:#b91c1c" data-infra-del="${escapeHtml(r.id)}">Удалить</button></td>
          </tr>`;
            })
            .join("");
        }
      }
    }
    void loadActivityLogs();
  };

  const renderInfraTemplateVars = () => {
    const wrap = document.getElementById("infra-template-vars");
    const hint = document.getElementById("infra-template-hint");
    if (!wrap) return;
    const entries = Object.entries(infraTemplateSchema || {});
    if (entries.length === 0) {
      wrap.innerHTML = `<p class="lp-muted" style="font-size:12px;">У выбранной роли нет обязательных пользовательских параметров.</p>`;
      if (hint) hint.textContent = "";
      return;
    }
    wrap.innerHTML = entries
      .map(([k, spec]) => {
        const req = spec?.required === true;
        const typ = String(spec?.type || "string").toLowerCase();
        const inputType = typ === "integer" || typ === "number" ? "number" : typ === "boolean" ? "checkbox" : "text";
        if (inputType === "checkbox") {
          return `<label class="lp-muted" style="font-size:12px;display:flex;align-items:center;gap:6px;">
            <input data-infra-var="${escapeHtml(k)}" data-infra-var-type="${escapeHtml(typ)}" type="checkbox" />
            ${escapeHtml(k)}${req ? " *" : ""}
          </label>`;
        }
        return `<div class="lp-field">
          <label>${escapeHtml(k)}${req ? " *" : ""}</label>
          <input class="lp-input" data-infra-var="${escapeHtml(k)}" data-infra-var-type="${escapeHtml(typ)}" type="${inputType}" />
        </div>`;
      })
      .join("");
    if (hint) hint.textContent = "Поля с * обычно требуются шаблоном. Базовые vars (ssh_user/server_host/project_*) заполняются автоматически.";
  };

  const loadInfraTemplateCatalog = async () => {
    const sel = document.getElementById("infra-template-id") as HTMLSelectElement | null;
    const hint = document.getElementById("infra-template-hint");
    if (!sel) return;
    if (!currentProjectId) {
      sel.innerHTML = `<option value="">— выберите проект —</option>`;
      infraTemplateCatalog = [];
      infraTemplateSchema = {};
      infraTemplateCatalogProjectId = "";
      renderInfraTemplateVars();
      return;
    }
    const r = await fetch(`${GATEWAY_BASE}/api/v1/ansible/swarm/catalog`, { headers: authHeader(token()) });
    if (!r.ok) {
      sel.innerHTML = `<option value="">— ошибка каталога (${r.status}) —</option>`;
      if (hint) hint.textContent = `Ошибка загрузки каталога: ${r.status}`;
      return;
    }
    const rows = (await r.json()) as InfraTemplateCatalogItem[];
    infraTemplateCatalog = Array.isArray(rows) ? rows : [];
    infraTemplateCatalogProjectId = currentProjectId;
    sel.innerHTML = `<option value="">— выберите роль —</option>${infraTemplateCatalog
      .map((x) => `<option value="${escapeHtml(x.id)}">${escapeHtml(x.name || x.id)}</option>`)
      .join("")}`;
    const first = infraTemplateCatalog[0];
    if (first) {
      sel.value = first.id;
      infraTemplateSchema = first.vars_schema || {};
    } else {
      infraTemplateSchema = {};
    }
    renderInfraTemplateVars();
  };

  const collectInfraTemplateVars = (): Record<string, unknown> => {
    const wrap = document.getElementById("infra-template-vars");
    const out: Record<string, unknown> = {};
    if (!wrap) return out;
    wrap.querySelectorAll("[data-infra-var]").forEach((el) => {
      const key = (el as HTMLElement).getAttribute("data-infra-var") || "";
      const typ = ((el as HTMLElement).getAttribute("data-infra-var-type") || "string").toLowerCase();
      if (!key) return;
      if (el instanceof HTMLInputElement && el.type === "checkbox") {
        out[key] = !!el.checked;
        return;
      }
      const raw = el instanceof HTMLInputElement ? el.value.trim() : "";
      if (!raw) return;
      if (typ === "integer" || typ === "number") {
        const n = Number(raw);
        if (Number.isFinite(n)) out[key] = n;
        return;
      }
      out[key] = raw;
    });
    return out;
  };

  const loadTargetsList = async () => {
    const tbody = document.getElementById("targets-table-body");
    if (!tbody) return;
    if (!currentProjectId) {
      tbody.innerHTML = `<tr><td colspan="10" class="lp-muted">Выберите проект</td></tr>`;
      void refreshInfraUi();
      return;
    }
    const r = await fetch(`${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/targets`, { headers: authHeader(token()) });
    if (!r.ok) {
      tbody.innerHTML = `<tr><td colspan="10" class="lp-muted">Ошибка ${r.status}</td></tr>`;
      void refreshInfraUi();
      return;
    }
    const rows = (await r.json()) as Array<{
      id: string;
      target_kind?: string;
      host: string;
      ssh_port: number;
      ssh_user: string;
      label: string;
      agent_status: string;
      agent_last_seen_at: string | null;
      agent_metrics_total: number;
      last_provision_ok: boolean | null;
      last_provision_at: string | null;
      last_provision_log: string | null;
    }>;
    if (rows.length === 0) {
      tbody.innerHTML = `<tr><td colspan="10" class="lp-muted">Нет зарегистрированных таргетов</td></tr>`;
      void refreshInfraUi();
      return;
    }
    const targetKindRu = (k?: string) => (k === "kubernetes" ? "K8s" : "SSH");
    const sshOnly = rows.filter((x) => x.target_kind !== "kubernetes");
    const k8sOpts = `<option value="">— выберите SSH-таргет —</option>${sshOnly
      .map((x) => `<option value="${escapeHtml(x.id)}">${escapeHtml(x.host)} (${escapeHtml(x.label)})</option>`)
      .join("")}`;
    const km = document.getElementById("k8s-master-target") as HTMLSelectElement | null;
    const kw1 = document.getElementById("k8s-worker1-target") as HTMLSelectElement | null;
    const kw2 = document.getElementById("k8s-worker2-target") as HTMLSelectElement | null;
    if (km) km.innerHTML = k8sOpts;
    if (kw1) kw1.innerHTML = k8sOpts;
    if (kw2) kw2.innerHTML = k8sOpts;
    const sshExecSel = document.getElementById("ssh-exec-target") as HTMLSelectElement | null;
    if (sshExecSel) {
      sshExecSel.innerHTML = `<option value="">— выберите SSH-сервер —</option>${sshOnly
        .map((x) => `<option value="${escapeHtml(x.id)}">${escapeHtml(x.host)} (${escapeHtml(x.label)})</option>`)
        .join("")}`;
    }
    const k8sRows = rows.filter((x) => x.target_kind === "kubernetes");
    const kmg = document.getElementById("k8s-mgmt-target") as HTMLSelectElement | null;
    if (kmg) {
      kmg.innerHTML = `<option value="">— выберите кластер —</option>${k8sRows
        .map((x) => `<option value="${escapeHtml(x.id)}">${escapeHtml(x.host)} (${escapeHtml(x.label)})</option>`)
        .join("")}`;
    }
    const opSel = document.getElementById("k8s-mgmt-op") as HTMLSelectElement | null;
    if (opSel && currentProjectId) {
      void fetch(`${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/k8s/operations`, { headers: authHeader(token()) }).then(
        async (or) => {
          if (or.ok) {
            const d = (await or.json()) as { operations?: string[] };
            const ops = d.operations || [];
            opSel.innerHTML = `<option value="">— операция —</option>${ops
              .map((o) => `<option value="${escapeHtml(o)}">${escapeHtml(o)}</option>`)
              .join("")}`;
          } else {
            opSel.innerHTML = `<option value="">(ошибка ${or.status})</option>`;
          }
        },
      );
    }
    tbody.innerHTML = rows
      .map((row) => {
        const log = row.last_provision_log || "";
        const logShort = log.length > 140 ? `${log.slice(0, 140)}…` : log;
        const logTitle = log.length > 2000 ? `${log.slice(0, 2000)}…` : log;
        return `<tr>
            <td style="padding:6px;border-bottom:1px solid #f1f5f9;">${escapeHtml(targetKindRu(row.target_kind))}</td>
            <td style="padding:6px;border-bottom:1px solid #f1f5f9;">${escapeHtml(row.host)} <span class="lp-muted">(${escapeHtml(row.label)})</span></td>
            <td style="padding:6px;border-bottom:1px solid #f1f5f9;">${row.ssh_port}</td>
            <td style="padding:6px;border-bottom:1px solid #f1f5f9;">${escapeHtml(row.ssh_user)}</td>
            <td style="padding:6px;border-bottom:1px solid #f1f5f9;">${escapeHtml(provisionInstallRu(row.last_provision_ok))}<br/><span class="lp-muted" style="font-size:11px;">${row.last_provision_at ? escapeHtml(row.last_provision_at) : ""}</span></td>
            <td style="padding:6px;border-bottom:1px solid #f1f5f9;max-width:240px;font-size:11px;white-space:pre-wrap;word-break:break-word;" title="${escapeHtml(logTitle)}">${log ? escapeHtml(logShort) : "—"}</td>
            <td style="padding:6px;border-bottom:1px solid #f1f5f9;">${row.agent_metrics_total}</td>
            <td style="padding:6px;border-bottom:1px solid #f1f5f9;">${escapeHtml(agentStatusRu(row.agent_status))}</td>
            <td style="padding:6px;border-bottom:1px solid #f1f5f9;">${row.agent_last_seen_at ? escapeHtml(row.agent_last_seen_at) : "—"}</td>
            <td style="padding:6px;border-bottom:1px solid #f1f5f9;"><button type="button" class="lp-btn lp-btn-ghost" style="color:#b91c1c;padding:4px 8px;font-size:12px" data-del-tid="${escapeHtml(row.id)}">Удалить</button></td>
          </tr>`;
      })
      .join("");
    void refreshInfraUi();
  };

  const loadProjectList = async () => {
    const sel = document.getElementById("project-select") as HTMLSelectElement | null;
    if (!sel) return;
    const t = token();
    const r = await fetch(`${GATEWAY_BASE}/api/v1/projects`, { headers: authHeader(t) });
    if (!r.ok) {
      sel.innerHTML = `<option value="">(${r.status} — нужен JWT)</option>`;
      return;
    }
    const rows = (await r.json()) as { id: string; name: string }[];
    sel.innerHTML = `<option value="">— выберите —</option>${rows.map((p) => `<option value="${p.id}">${escapeHtml(p.name)}</option>`).join("")}`;
    const hasCurrent = !!currentProjectId && rows.some((p) => p.id === currentProjectId);
    if (hasCurrent) {
      sel.value = currentProjectId;
    } else if (currentProjectId) {
      currentProjectId = "";
      writeSelectedProjectId("");
      sel.value = "";
    }
    loadProjectAiChatForProject(currentProjectId);
    renderProjectAiChat();
    setProjectAiChatBusy(false);
    restoreOrchestrationForProject();
    void loadTargetsList();
  };

  const refreshStatus = async () => {
    if (!currentProjectId) {
      const el = document.getElementById("project-status-label");
      if (el) el.textContent = "—";
      return;
    }
    const r = await fetch(`${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/status`, { headers: authHeader(token()) });
    const data = (await r.json()) as { status?: string };
    const el = document.getElementById("project-status-label");
    if (el) el.textContent = data.status || "?";
  };

  const refreshWorkspaceHint = async () => {
    if (!currentProjectId) return;
    const r = await fetch(`${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/workspace`, { headers: authHeader(token()) });
    const hint = document.getElementById("workspace-hint");
    if (!r.ok) {
      if (hint) hint.textContent = "workspace: ошибка";
      return;
    }
    const w = (await r.json()) as { can_use_llm_chat?: boolean; agent_metrics_count?: number };
    const ok = !!w.can_use_llm_chat;
    if (hint) {
      hint.textContent = ok
        ? "Агент на сервере: ок (метрики есть или настройка подтверждена)."
        : `Агент: метрик ${w.agent_metrics_count ?? 0} — для установки агента сохраните SSH или подтвердите настройку вручную.`;
    }
  };

  void refreshSession();
  void loadProjectList();

  document.getElementById("portal-logout")?.addEventListener("click", () => {
    localStorage.removeItem(LS_USER_JWT);
    location.reload();
  });

  document.getElementById("portal-refresh-projects")?.addEventListener("click", () => void loadProjectList());

  document.getElementById("project-select")?.addEventListener("change", (ev) => {
    currentProjectId = (ev.target as HTMLSelectElement).value;
    writeSelectedProjectId(currentProjectId);
    loadProjectAiChatForProject(currentProjectId);
    renderProjectAiChat();
    setProjectAiChatBusy(false);
    restoreOrchestrationForProject();
    void refreshStatus();
    void refreshWorkspaceHint();
    void loadTargetsList();
  });

  document.getElementById("refresh-status")?.addEventListener("click", () => void refreshStatus());

  document.getElementById("load-catalog")?.addEventListener("click", async () => {
    const t = tokenInput?.value.trim() || "";
    state.userToken = t;
    const resp = await fetch(`${GATEWAY_BASE}/api/v1/catalog`, { headers: authHeader(t || token()) });
    write(await resp.json());
  });

  document.getElementById("create-project")?.addEventListener("click", async () => {
    const name = (document.getElementById("project-name") as HTMLInputElement).value;
    const description = (document.getElementById("project-desc") as HTMLTextAreaElement).value;
    const tenant_id = (document.getElementById("tenant-id") as HTMLInputElement).value;
    const authTok = token();
    const body: Record<string, string> = { name, description, tenant_id };
    const resp = await fetch(`${GATEWAY_BASE}/api/v1/projects`, {
      method: "POST",
      headers: { ...authHeader(authTok), "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const payload = (await resp.json()) as { id?: string; detail?: string };
    if (!resp.ok) {
      write(payload);
      return;
    }
    currentProjectId = payload.id || "";
    writeSelectedProjectId(currentProjectId);
    await loadProjectList();
    const sel = document.getElementById("project-select") as HTMLSelectElement | null;
    if (sel && currentProjectId) sel.value = currentProjectId;
    loadProjectAiChatForProject(currentProjectId);
    renderProjectAiChat();
    setProjectAiChatBusy(false);
    restoreOrchestrationForProject();
    write(payload);
    void refreshStatus();
    void refreshWorkspaceHint();
    void loadTargetsList();
  });

  document.getElementById("refresh-targets-list")?.addEventListener("click", () => void loadTargetsList());

  document.getElementById("infra-save-assignments")?.addEventListener("click", async () => {
    if (!currentProjectId) {
      write({ error: "Выберите проект" });
      return;
    }
    const gl = document.getElementById("infra-gitlab-target") as HTMLSelectElement | null;
    const nx = document.getElementById("infra-nexus-target") as HTMLSelectElement | null;
    const body = {
      assignments: {
        gitlab: gl?.value || null,
        nexus: nx?.value || null,
      },
    };
    const resp = await fetch(`${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/infra/service-assignments`, {
      method: "PUT",
      headers: { ...authHeader(token()), "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    write(await resp.json());
    void refreshInfraUi();
  });

  document.getElementById("infra-prepare-stack")?.addEventListener("click", async () => {
    if (!currentProjectId) {
      write({ error: "Выберите проект" });
      return;
    }
    const gp = Number((document.getElementById("infra-gitlab-port") as HTMLInputElement).value || 8081);
    const np = Number((document.getElementById("infra-nexus-port") as HTMLInputElement).value || 8082);
    const resp = await fetch(`${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/infra/prepare-stack`, {
      method: "POST",
      headers: { ...authHeader(token()), "Content-Type": "application/json" },
      body: JSON.stringify({ gitlab_http_port: gp, nexus_http_port: np }),
    });
    const data = await resp.json();
    write(data);
    void refreshInfraUi();
    void loadActivityLogs();
  });

  const infraAnsibleLogMax = 450_000;

  document.getElementById("infra-run-stack")?.addEventListener("click", async () => {
    if (!currentProjectId) {
      write({ error: "Выберите проект" });
      return;
    }
    if (!confirm("Запустить ansible-playbook на сервере платформы по SSH к вашим хостам? Первый запуск GitLab может занять 10+ минут.")) {
      return;
    }
    const gp = Number((document.getElementById("infra-gitlab-port") as HTMLInputElement).value || 8081);
    const np = Number((document.getElementById("infra-nexus-port") as HTMLInputElement).value || 8082);
    const statusEl = document.getElementById("infra-ansible-status");
    const logPre = document.getElementById("infra-ansible-log");
    const runBtn = document.getElementById("infra-run-stack") as HTMLButtonElement | null;
    const bodyJson = JSON.stringify({ gitlab_http_port: gp, nexus_http_port: np });

    const setStatus = (text: string) => {
      if (statusEl) statusEl.textContent = text;
    };
    const appendLog = (s: string) => {
      if (!logPre) return;
      let t = logPre.textContent || "";
      t += s;
      if (t.length > infraAnsibleLogMax) {
        t = `… [обрезано]\n${t.slice(-infraAnsibleLogMax)}`;
      }
      logPre.textContent = t;
      logPre.scrollTop = logPre.scrollHeight;
    };

    if (logPre) logPre.textContent = "";
    setStatus("Ansible: подключение…");
    if (runBtn) runBtn.disabled = true;

    try {
      const resp = await fetch(`${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/infra/run-stack-stream`, {
        method: "POST",
        headers: { ...authHeader(token()), "Content-Type": "application/json" },
        body: bodyJson,
      });
      if (!resp.ok) {
        const errText = await resp.text();
        let detail: unknown = errText;
        try {
          detail = JSON.parse(errText) as unknown;
        } catch {
          /* raw text */
        }
        write({ error: "run-stack-stream", status: resp.status, detail });
        setStatus(`Ansible: ошибка HTTP ${resp.status}`);
        appendLog(`${errText}\n`);
        return;
      }
      if (!resp.body) {
        write({ error: "Нет тела ответа (stream)" });
        setStatus("Ansible: ошибка потока");
        return;
      }
      const reader = resp.body.getReader();
      const dec = new TextDecoder();
      let buffer = "";
      let finalPayload: unknown = null;
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += dec.decode(value, { stream: true });
        const parts = buffer.split("\n");
        buffer = parts.pop() ?? "";
        for (const raw of parts) {
          const line = raw.trim();
          if (!line) continue;
          let ev: Record<string, unknown>;
          try {
            ev = JSON.parse(line) as Record<string, unknown>;
          } catch {
            appendLog(`[parse error] ${line}\n`);
            continue;
          }
          const typ = String(ev.type || "");
          if (typ === "phase") {
            const step = String(ev.step || "");
            const host = String(ev.host || "");
            const sr = ev.service_role != null ? String(ev.service_role) : "";
            const tid = String(ev.template_id || "");
            if (step === "prepare-docker-host") {
              setStatus(`Ansible: подготовка Docker на ${host}…`);
            } else {
              setStatus(`Ansible: ${sr || "роль"} (${tid}) на ${host}…`);
            }
          } else if (typ === "line") {
            appendLog(`${String(ev.text ?? "")}\n`);
          } else if (typ === "phase_done") {
            const ok = ev.ok === true;
            const rc = ev.ansible_rc;
            setStatus(
              ok
                ? `Ansible: шаг завершён (rc=${String(rc)})`
                : `Ansible: шаг с ошибкой (rc=${String(rc)})`,
            );
          } else if (typ === "skip") {
            setStatus(`Ansible: пропуск — ${String(ev.reason || "")}`);
          } else if (typ === "bundle_error") {
            setStatus(`Ansible: ошибка bundle — ${String(ev.detail || ev.step || "")}`);
            appendLog(`[bundle_error] ${JSON.stringify(ev)}\n`);
          } else if (typ === "done") {
            finalPayload = ev.payload;
            const st = String(ev.summary || ev.status || "");
            setStatus(st ? `Ansible: ${st}` : "Ansible: готово");
          }
        }
      }
      if (buffer.trim()) {
        try {
          const ev = JSON.parse(buffer) as Record<string, unknown>;
          if (ev.type === "done") {
            finalPayload = ev.payload;
          }
        } catch {
          /* incomplete */
        }
      }
      if (finalPayload !== null) {
        write(finalPayload);
      } else {
        write({ warning: "Поток завершён без события done", hint: "Смотрите лог выше" });
      }
      void refreshInfraUi();
      void loadActivityLogs();
    } catch (e) {
      write({ error: String(e) });
      setStatus("Ansible: сбой запроса");
    } finally {
      if (runBtn) runBtn.disabled = false;
    }
  });

  document.getElementById("infra-template-reload")?.addEventListener("click", () => void loadInfraTemplateCatalog());

  document.getElementById("infra-template-id")?.addEventListener("change", (ev) => {
    const id = (ev.target as HTMLSelectElement).value;
    const found = infraTemplateCatalog.find((x) => x.id === id);
    infraTemplateSchema = found?.vars_schema || {};
    renderInfraTemplateVars();
  });

  document.getElementById("infra-template-run")?.addEventListener("click", async () => {
    if (!currentProjectId) {
      write({ error: "Выберите проект" });
      return;
    }
    const targetId = (document.getElementById("infra-template-target") as HTMLSelectElement | null)?.value || "";
    const templateId = (document.getElementById("infra-template-id") as HTMLSelectElement | null)?.value || "";
    if (!targetId || !templateId) {
      write({ error: "Выберите SSH-таргет и роль" });
      return;
    }
    const vars = collectInfraTemplateVars();
    const allowFallback = (document.getElementById("infra-template-fallback") as HTMLInputElement | null)?.checked === true;
    const fallbackService =
      (document.getElementById("infra-template-fallback-service") as HTMLSelectElement | null)?.value || "gitlab";
    const gp = Number((document.getElementById("infra-gitlab-port") as HTMLInputElement).value || 8081);
    const np = Number((document.getElementById("infra-nexus-port") as HTMLInputElement).value || 8082);
    const runBtn = document.getElementById("infra-template-run") as HTMLButtonElement | null;
    const statusEl = document.getElementById("infra-ansible-status");
    const hint = document.getElementById("infra-template-hint");
    if (runBtn) runBtn.disabled = true;
    if (statusEl) statusEl.textContent = `Ansible: запуск ${templateId}…`;
    try {
      const resp = await fetch(`${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/infra/run-template`, {
        method: "POST",
        headers: { ...authHeader(token()), "Content-Type": "application/json" },
        body: JSON.stringify({
          target_id: targetId,
          template_id: templateId,
          variables: vars,
          gitlab_http_port: gp,
          nexus_http_port: np,
          allow_fallback: allowFallback,
          fallback_service: fallbackService,
        }),
      });
      const raw = await resp.text();
      let data: unknown = raw;
      try {
        data = JSON.parse(raw) as unknown;
      } catch {
        /* keep text */
      }
      write(data);
      const obj = (data && typeof data === "object" ? (data as Record<string, unknown>) : null);
      const okFlag = obj && typeof obj.ok === "boolean" ? obj.ok : undefined;
      const detailText =
        (obj && typeof obj.detail === "string" && obj.detail.trim()) ||
        (obj && typeof obj.error === "string" && obj.error.trim()) ||
        "";
      if (resp.ok && okFlag !== false) {
        if (statusEl) statusEl.textContent = `Ansible: ${templateId} завершён`;
        if (hint) hint.textContent = "Роль выполнена. Смотрите журнал и ресурсы ниже.";
      } else {
        if (statusEl) statusEl.textContent = `Ansible: ошибка ${resp.status}`;
        if (hint) hint.textContent = detailText ? `Ошибка запуска: ${detailText}` : `Ошибка запуска: ${resp.status}`;
      }
      void refreshInfraUi();
      void loadActivityLogs();
    } catch (e) {
      write({ error: String(e) });
      if (statusEl) statusEl.textContent = "Ansible: сбой запроса";
      if (hint) hint.textContent = String(e);
    } finally {
      if (runBtn) runBtn.disabled = false;
    }
  });

  const k8sInstallLogMax = 450_000;

  document.getElementById("k8s-install-run")?.addEventListener("click", async () => {
    if (!currentProjectId) {
      write({ error: "Выберите проект" });
      return;
    }
    const master = (document.getElementById("k8s-master-target") as HTMLSelectElement).value;
    const w1 = (document.getElementById("k8s-worker1-target") as HTMLSelectElement).value;
    const w2 = (document.getElementById("k8s-worker2-target") as HTMLSelectElement).value;
    const pod = (document.getElementById("k8s-pod-cidr") as HTMLInputElement).value.trim() || "10.244.0.0/16";
    if (!master || !w1 || !w2) {
      write({ error: "Выберите три разных SSH-таргета" });
      return;
    }
    if (new Set([master, w1, w2]).size !== 3) {
      write({ error: "Нужны три разных сервера (control plane и два worker)" });
      return;
    }
    if (
      !confirm(
        "Запустить установку Kubernetes (kubeadm) на выбранных серверах через Ansible на платформе? Это долго и изменит ОС.",
      )
    ) {
      return;
    }
    const statusEl = document.getElementById("k8s-install-status");
    const logPre = document.getElementById("k8s-install-log");
    const runBtn = document.getElementById("k8s-install-run") as HTMLButtonElement | null;
    const bodyJson = JSON.stringify({
      master_target_id: master,
      worker1_target_id: w1,
      worker2_target_id: w2,
      pod_network_cidr: pod,
    });
    const setStatus = (text: string) => {
      if (statusEl) statusEl.textContent = text;
    };
    const appendLog = (s: string) => {
      if (!logPre) return;
      let t = logPre.textContent || "";
      t += s;
      if (t.length > k8sInstallLogMax) {
        t = `… [обрезано]\n${t.slice(-k8sInstallLogMax)}`;
      }
      logPre.textContent = t;
      logPre.scrollTop = logPre.scrollHeight;
    };
    if (logPre) logPre.textContent = "";
    setStatus("Kubernetes: подключение…");
    if (runBtn) runBtn.disabled = true;
    try {
      const resp = await fetch(`${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/infra/k8s-install-3node-stream`, {
        method: "POST",
        headers: { ...authHeader(token()), "Content-Type": "application/json" },
        body: bodyJson,
      });
      if (!resp.ok) {
        const errText = await resp.text();
        write({ error: "k8s-install-stream", status: resp.status, detail: errText });
        setStatus(`Kubernetes: ошибка HTTP ${resp.status}`);
        appendLog(`${errText}\n`);
        return;
      }
      if (!resp.body) {
        write({ error: "Нет тела ответа (stream)" });
        setStatus("Kubernetes: ошибка потока");
        return;
      }
      const reader = resp.body.getReader();
      const dec = new TextDecoder();
      let buffer = "";
      let finalPayload: unknown = null;
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += dec.decode(value, { stream: true });
        const parts = buffer.split("\n");
        buffer = parts.pop() ?? "";
        for (const raw of parts) {
          const line = raw.trim();
          if (!line) continue;
          let ev: Record<string, unknown>;
          try {
            ev = JSON.parse(line) as Record<string, unknown>;
          } catch {
            appendLog(`[parse error] ${line}\n`);
            continue;
          }
          const typ = String(ev.type || "");
          if (typ === "phase") {
            const host = String(ev.host || "");
            const tid = String(ev.template_id || "");
            setStatus(`Kubernetes: ${tid} на ${host}…`);
          } else if (typ === "line") {
            appendLog(`${String(ev.text ?? "")}\n`);
          } else if (typ === "done") {
            finalPayload = ev.payload;
            setStatus(String(ev.summary || ev.status || "Kubernetes: готово"));
          }
        }
      }
      if (buffer.trim()) {
        try {
          const ev = JSON.parse(buffer) as Record<string, unknown>;
          if (ev.type === "done") finalPayload = ev.payload;
        } catch {
          /* incomplete */
        }
      }
      if (finalPayload !== null) write(finalPayload);
      else write({ warning: "Поток завершён без события done" });
      void loadActivityLogs();
    } catch (e) {
      write({ error: String(e) });
      setStatus("Kubernetes: сбой запроса");
    } finally {
      if (runBtn) runBtn.disabled = false;
    }
  });

  document.getElementById("k8s-mgmt-run")?.addEventListener("click", async () => {
    if (!currentProjectId) {
      write({ error: "Выберите проект" });
      return;
    }
    const tid = (document.getElementById("k8s-mgmt-target") as HTMLSelectElement).value;
    const op = (document.getElementById("k8s-mgmt-op") as HTMLSelectElement).value;
    if (!tid || !op) {
      write({ error: "Выберите кластер и операцию" });
      return;
    }
    const body: Record<string, unknown> = {
      target_id: tid,
      operation: op,
      tail_lines: Number((document.getElementById("k8s-mgmt-tail") as HTMLInputElement).value || 200),
      all_namespaces: (document.getElementById("k8s-mgmt-allns") as HTMLInputElement).checked,
      wide: (document.getElementById("k8s-mgmt-wide") as HTMLInputElement).checked,
    };
    const ns = (document.getElementById("k8s-mgmt-ns") as HTMLInputElement).value.trim();
    if (ns) body.namespace = ns;
    const nm = (document.getElementById("k8s-mgmt-name") as HTMLInputElement).value.trim();
    if (nm) body.name = nm;
    const res = (document.getElementById("k8s-mgmt-res") as HTMLInputElement).value.trim();
    if (res) body.resource = res;
    const ctr = (document.getElementById("k8s-mgmt-ctr") as HTMLInputElement).value.trim();
    if (ctr) body.container = ctr;
    const repRaw = (document.getElementById("k8s-mgmt-rep") as HTMLInputElement).value.trim();
    if (repRaw !== "") {
      const n = Number(repRaw);
      if (!Number.isNaN(n)) body.replicas = n;
    }
    const outEl = document.getElementById("k8s-mgmt-out");
    if (outEl) outEl.textContent = "…";
    try {
      const resp = await fetch(`${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/k8s/query`, {
        method: "POST",
        headers: { ...authHeader(token()), "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = (await resp.json()) as Record<string, unknown>;
      if (outEl) outEl.textContent = JSON.stringify(data, null, 2);
      write(data);
    } catch (e) {
      write({ error: String(e) });
      if (outEl) outEl.textContent = String(e);
    }
  });

  document.getElementById("infra-load-context")?.addEventListener("click", async () => {
    if (!currentProjectId) {
      write({ error: "Выберите проект" });
      return;
    }
    const resp = await fetch(`${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/development-context`, {
      headers: authHeader(token()),
    });
    write(await resp.json());
  });

  document.getElementById("agent-demo-run")?.addEventListener("click", async () => {
    if (!currentProjectId) {
      write({ error: "Выберите проект" });
      return;
    }
    const setAns = (t: string) => {
      const el = document.getElementById("infra-ansible-status");
      if (el) el.textContent = t;
    };
    setAns("Демо агентов: запрос…");
    try {
      const resp = await fetch(`${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/agent-demo/run`, {
        method: "POST",
        headers: { ...authHeader(token()), "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      const data = await resp.json();
      write(data);
      if (resp.ok && data.ok) {
        setAns("Демо агентов: готово — см. JSON и журнал");
      } else {
        setAns(`Демо агентов: ошибка (${resp.status})`);
      }
    } catch (e) {
      write({ error: String(e) });
      setAns("Демо агентов: сбой запроса");
    }
    void loadActivityLogs();
  });

  document.getElementById("agents-bootstrap-planning")?.addEventListener("click", async () => {
    if (!currentProjectId) {
      write({ error: "Выберите проект" });
      return;
    }
    const setAns = (t: string) => {
      const el = document.getElementById("infra-ansible-status");
      if (el) el.textContent = t;
    };
    setAns("Planning bootstrap…");
    try {
      const resp = await fetch(`${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/agents/bootstrap-planning`, {
        method: "POST",
        headers: { ...authHeader(token()), "Content-Type": "application/json" },
        body: JSON.stringify({ force: false }),
      });
      const data = await resp.json();
      write(data);
      setAns(resp.ok ? "Bootstrap: OK" : `Bootstrap: ${resp.status}`);
    } catch (e) {
      write({ error: String(e) });
      setAns("Bootstrap: ошибка");
    }
    void refreshStatus();
  });

  const sendProjectAiChat = async () => {
    if (!currentProjectId) {
      const g = document.getElementById("project-ai-chat-gate");
      if (g) g.textContent = "Сначала выберите проект в списке выше.";
      return;
    }
    const inp = document.getElementById("project-ai-chat-input") as HTMLTextAreaElement | null;
    const userMsg = inp?.value?.trim() ?? "";
    if (!userMsg) return;
    await startOrchestration();
    void refreshStatus();
    void refreshWorkspaceHint();
  };

  document.getElementById("project-ai-chat-send")?.addEventListener("click", () => void sendProjectAiChat());
  document.getElementById("project-orchestrate-btn")?.addEventListener("click", () => void startOrchestration());
  document.getElementById("orch-node-modal-close")?.addEventListener("click", () => closeOrchNodeModal());
  document.getElementById("orchestration-node-modal")?.addEventListener("click", (ev) => {
    const t = ev.target as HTMLElement | null;
    if (t && t.id === "orchestration-node-modal") {
      closeOrchNodeModal();
    }
  });
  document.getElementById("tracker-task-modal-close")?.addEventListener("click", () => closeTrackerTaskModal());
  document.getElementById("tracker-task-modal")?.addEventListener("click", (ev) => {
    const t = ev.target as HTMLElement | null;
    if (t && t.id === "tracker-task-modal") {
      closeTrackerTaskModal();
    }
  });

  document.getElementById("docs-refresh-btn")?.addEventListener("click", () => void loadDocumentsList());
  document.getElementById("docs-new-btn")?.addEventListener("click", () => resetDocsEditor());
  document.getElementById("docs-save-btn")?.addEventListener("click", () => void saveDocumentFromEditor());
  document.getElementById("docs-search-btn")?.addEventListener("click", () => void searchDocuments());
  document.getElementById("docs-search-query")?.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") {
      ev.preventDefault();
      void searchDocuments();
    }
  });
  document.getElementById("docs-filter-type")?.addEventListener("change", () => void loadDocumentsList());

  document.getElementById("tracker-create-btn")?.addEventListener("click", async () => {
    if (!currentProjectId) {
      alert("Выберите проект");
      return;
    }
    const kind = (document.getElementById("tracker-new-kind") as HTMLSelectElement).value;
    const title = (document.getElementById("tracker-new-title") as HTMLInputElement).value.trim();
    const desc = (document.getElementById("tracker-new-desc") as HTMLTextAreaElement).value.trim();
    const role = (document.getElementById("tracker-new-role") as HTMLSelectElement).value;
    if (!title) {
      alert("Укажите заголовок");
      return;
    }
    if (kind === "task" && !role) {
      alert("Для задачи выберите роль агента");
      return;
    }
    const body: Record<string, unknown> = { kind, title, description: desc };
    if (role) body.assignee_role = role;
    const resp = await fetch(`${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/tracker/items`, {
      method: "POST",
      headers: { ...authHeader(token()), "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const raw = await resp.text();
    if (!resp.ok) {
      alert(raw || String(resp.status));
      return;
    }
    (document.getElementById("tracker-new-title") as HTMLInputElement).value = "";
    (document.getElementById("tracker-new-desc") as HTMLTextAreaElement).value = "";
    void loadTrackerItems();
  });
  document.getElementById("tracker-refresh-btn")?.addEventListener("click", () => void loadTrackerItems());

  document.getElementById("tracker-edit-cancel")?.addEventListener("click", () => {
    const w = document.getElementById("tracker-edit-wrap");
    if (w) w.style.display = "none";
  });

  document.getElementById("tracker-edit-save")?.addEventListener("click", async () => {
    const id = (document.getElementById("tracker-edit-id") as HTMLInputElement).value.trim();
    const kind = (document.getElementById("tracker-edit-kind") as HTMLInputElement).value;
    const title = (document.getElementById("tracker-edit-title") as HTMLInputElement).value.trim();
    const desc = (document.getElementById("tracker-edit-desc") as HTMLTextAreaElement).value;
    const role = (document.getElementById("tracker-edit-role") as HTMLSelectElement).value;
    if (!id || !currentProjectId) return;
    if (!title) {
      alert("Укажите заголовок");
      return;
    }
    if (kind === "task" && !role) {
      alert("Для задачи укажите роль агента");
      return;
    }
    const body: Record<string, unknown> = { title, description: desc };
    if (kind === "task") {
      body.assignee_role = role;
    }
    try {
      const resp = await fetch(
        `${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/tracker/items/${encodeURIComponent(id)}`,
        {
          method: "PATCH",
          headers: { ...authHeader(token()), "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
      );
      const raw = await resp.text();
      if (!resp.ok) {
        alert(raw || String(resp.status));
        return;
      }
    } catch (e) {
      alert(String(e));
      return;
    }
    const w = document.getElementById("tracker-edit-wrap");
    if (w) w.style.display = "none";
    void loadTrackerItems();
  });
  document.getElementById("project-ai-chat-clear")?.addEventListener("click", () => {
    if (!currentProjectId) return;
    if (!confirm("Очистить историю чата для этого проекта?")) return;
    stopOrchestrationPoll();
    stopOrchestrationStream();
    projectAiChatHistory = [];
    sessionStorage.removeItem(projectAiChatStorageKey(currentProjectId));
    sessionStorage.removeItem(`lambot_orch_run_${currentProjectId}`);
    /* Ключи syncOrchestrationStepsToChat не сбрасываем: иначе следующий poll снова подтянет шаги ИИ Команды в чат. */
    renderProjectAiChat();
  });
  document.getElementById("project-ai-chat-input")?.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) {
      ev.preventDefault();
      void sendProjectAiChat();
    }
  });

  loadProjectAiChatForProject(currentProjectId);
  renderProjectAiChat();
  setProjectAiChatBusy(false);
  restoreOrchestrationForProject();

  document.getElementById("sandbox-run")?.addEventListener("click", async () => {
    if (!currentProjectId) {
      write({ error: "Выберите проект" });
      return;
    }
    const code = (document.getElementById("sandbox-code") as HTMLTextAreaElement)?.value ?? "";
    if (!code.trim()) {
      write({ error: "Введите код" });
      return;
    }
    try {
      const resp = await fetch(`${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/sandbox/python`, {
        method: "POST",
        headers: { ...authHeader(token()), "Content-Type": "application/json" },
        body: JSON.stringify({ code, timeout_sec: 20 }),
      });
      write(await resp.json());
    } catch (e) {
      write({ error: String(e) });
    }
  });

  document.getElementById("infra-refresh-resources")?.addEventListener("click", () => void refreshInfraUi());

  const clearInfraServiceForm = () => {
    const n = document.getElementById("infra-service-name") as HTMLInputElement | null;
    const h = document.getElementById("infra-service-host") as HTMLInputElement | null;
    const u = document.getElementById("infra-service-user") as HTMLInputElement | null;
    const s = document.getElementById("infra-service-secret") as HTMLInputElement | null;
    const e = document.getElementById("infra-service-enabled") as HTMLInputElement | null;
    const hid = document.getElementById("infra-service-edit-id") as HTMLInputElement | null;
    const cancel = document.getElementById("infra-service-cancel-edit") as HTMLElement | null;
    const saveBtn = document.getElementById("infra-service-save") as HTMLButtonElement | null;
    const hint = document.getElementById("infra-service-form-hint");
    if (n) n.value = "";
    if (h) h.value = "";
    if (u) u.value = "";
    if (s) s.value = "";
    if (e) e.checked = true;
    if (hid) hid.value = "";
    if (cancel) cancel.style.display = "none";
    if (saveBtn) saveBtn.textContent = "Добавить сервис";
    if (hint) {
      hint.style.display = "none";
      hint.textContent = "";
    }
  };

  document.getElementById("infra-service-cancel-edit")?.addEventListener("click", () => clearInfraServiceForm());

  document.getElementById("infra-service-save")?.addEventListener("click", async () => {
    if (!currentProjectId) {
      write({ error: "Выберите проект" });
      return;
    }
    const name = (document.getElementById("infra-service-name") as HTMLInputElement).value.trim();
    const host = (document.getElementById("infra-service-host") as HTMLInputElement).value.trim();
    const user = (document.getElementById("infra-service-user") as HTMLInputElement).value.trim();
    const secret = (document.getElementById("infra-service-secret") as HTMLInputElement).value;
    const enabled = (document.getElementById("infra-service-enabled") as HTMLInputElement).checked;
    const editId = (document.getElementById("infra-service-edit-id") as HTMLInputElement).value.trim();
    if (!name || !host) {
      alert("Укажите название и IP/FQDN");
      return;
    }
    const value_json: Record<string, unknown> = { host, user, enabled };
    const hAuth = { ...authHeader(token()), "Content-Type": "application/json" };
    try {
      if (editId) {
        const body: Record<string, unknown> = { label: name, value_json };
        if (secret) body.secret_plain = secret;
        const resp = await fetch(
          `${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/infra/resources/${encodeURIComponent(editId)}`,
          { method: "PATCH", headers: hAuth, body: JSON.stringify(body) },
        );
        const raw = await resp.text();
        if (!resp.ok) {
          alert(raw || String(resp.status));
          return;
        }
      } else {
        const body: Record<string, unknown> = {
          category: "service",
          label: name,
          value_json,
        };
        if (secret) body.secret_plain = secret;
        const resp = await fetch(`${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/infra/resources`, {
          method: "POST",
          headers: hAuth,
          body: JSON.stringify(body),
        });
        const raw = await resp.text();
        if (!resp.ok) {
          alert(raw || String(resp.status));
          return;
        }
      }
    } catch (e) {
      alert(String(e));
      return;
    }
    clearInfraServiceForm();
    void refreshInfraUi();
  });

  document.getElementById("infra-resources-wrap")?.addEventListener("change", async (ev) => {
    const t = ev.target as HTMLElement;
    if (!(t instanceof HTMLInputElement) || !t.hasAttribute("data-infra-enabled")) return;
    const id = t.getAttribute("data-infra-enabled");
    if (!id || !currentProjectId) return;
    try {
      const resp = await fetch(
        `${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/infra/resources/${encodeURIComponent(id)}`,
        {
          method: "PATCH",
          headers: { ...authHeader(token()), "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: t.checked }),
        },
      );
      if (!resp.ok) {
        const raw = await resp.text();
        alert(raw || String(resp.status));
        t.checked = !t.checked;
        return;
      }
    } catch (e) {
      alert(String(e));
      t.checked = !t.checked;
      return;
    }
  });

  document.getElementById("infra-resources-wrap")?.addEventListener("click", async (ev) => {
    const delBtn = (ev.target as HTMLElement).closest("button[data-infra-del]");
    if (delBtn) {
      const id = delBtn.getAttribute("data-infra-del");
      if (!id || !currentProjectId) return;
      if (!confirm("Удалить эту запись? Секрет будет удалён из проекта.")) return;
      try {
        const resp = await fetch(
          `${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/infra/resources/${encodeURIComponent(id)}`,
          { method: "DELETE", headers: authHeader(token()) },
        );
        if (!resp.ok) {
          alert(await resp.text());
          return;
        }
      } catch (e) {
        alert(String(e));
        return;
      }
      clearInfraServiceForm();
      void refreshInfraUi();
      return;
    }
    const editBtn = (ev.target as HTMLElement).closest("button[data-infra-edit]");
    if (!editBtn || !currentProjectId) return;
    const id = editBtn.getAttribute("data-infra-edit");
    if (!id) return;
    try {
      const resp = await fetch(`${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/infra/resources`, {
        headers: authHeader(token()),
      });
      if (!resp.ok) {
        alert(await resp.text());
        return;
      }
      const list = (await resp.json()) as Array<{
        id: string;
        category: string;
        label: string;
        value_json: Record<string, unknown>;
        enabled?: boolean;
      }>;
      const row = list.find((x) => x.id === id);
      if (!row || row.category !== "service") return;
      (document.getElementById("infra-service-name") as HTMLInputElement).value = row.label;
      (document.getElementById("infra-service-host") as HTMLInputElement).value = String(row.value_json?.host ?? row.value_json?.fqdn ?? "");
      (document.getElementById("infra-service-user") as HTMLInputElement).value = String(row.value_json?.user ?? "");
      (document.getElementById("infra-service-secret") as HTMLInputElement).value = "";
      (document.getElementById("infra-service-enabled") as HTMLInputElement).checked = row.enabled !== false;
      (document.getElementById("infra-service-edit-id") as HTMLInputElement).value = id;
      (document.getElementById("infra-service-save") as HTMLButtonElement).textContent = "Сохранить";
      (document.getElementById("infra-service-cancel-edit") as HTMLElement).style.display = "inline-flex";
      const hint = document.getElementById("infra-service-form-hint");
      if (hint) {
        hint.style.display = "block";
        hint.textContent = "Пароль/API key укажите только если меняете; пустое поле оставит прежний секрет.";
      }
    } catch (e) {
      alert(String(e));
    }
  });

  document.getElementById("activity-logs-refresh")?.addEventListener("click", () => void loadActivityLogs());
  document.getElementById("activity-logs-wrap")?.addEventListener("click", (ev) => {
    const tr = (ev.target as HTMLElement).closest("tr[data-act-idx]");
    if (!tr) return;
    const idx = Number(tr.getAttribute("data-act-idx"));
    const it = lastActivityItems[idx];
    const det = document.getElementById("activity-logs-detail");
    if (det && it) det.textContent = JSON.stringify(it, null, 2);
  });

  document.getElementById("targets-table-wrap")?.addEventListener("click", async (ev) => {
    const btn = (ev.target as HTMLElement).closest("button[data-del-tid]");
    if (!btn) return;
    const tid = btn.getAttribute("data-del-tid");
    if (!tid || !currentProjectId) return;
    if (!confirm("Удалить этот SSH-сервер из проекта? Метрики агента для него будут удалены.")) return;
    const resp = await fetch(`${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/targets/${encodeURIComponent(tid)}`, {
      method: "DELETE",
      headers: authHeader(token()),
    });
    const data = await resp.json();
    write(data);
    if (!resp.ok) return;
    if (currentTargetId === tid) currentTargetId = "";
    void loadTargetsList();
    void refreshWorkspaceHint();
  });

  document.getElementById("load-swarm-catalog")?.addEventListener("click", async () => {
    await loadInfraTemplateCatalog();
    const resp = await fetch(`${GATEWAY_BASE}/api/v1/ansible/swarm/catalog`, { headers: authHeader(token()) });
    write(await resp.json());
  });

  const syncSrvKindUi = () => {
    const kind = (document.getElementById("srv-kind") as HTMLSelectElement | null)?.value || "ssh";
    const sshB = document.getElementById("srv-ssh-block");
    const k8sB = document.getElementById("srv-k8s-block");
    if (sshB) sshB.style.display = kind === "ssh" ? "" : "none";
    if (k8sB) k8sB.style.display = kind === "kubernetes" ? "" : "none";
  };

  document.getElementById("srv-kind")?.addEventListener("change", () => syncSrvKindUi());
  syncSrvKindUi();

  const buildTargetBody = (): Record<string, unknown> => {
    const kind = (document.getElementById("srv-kind") as HTMLSelectElement | null)?.value || "ssh";
    if (kind === "kubernetes") {
      const name = (document.getElementById("srv-k8s-name") as HTMLInputElement).value.trim() || "Мой кластер";
      return {
        target_kind: "kubernetes",
        host: name,
        ssh_port: 0,
        ssh_user: "kubernetes",
        auth_type: "kubeconfig",
        kubeconfig: (document.getElementById("srv-k8s-config") as HTMLTextAreaElement).value,
        kube_context: (document.getElementById("srv-k8s-context") as HTMLInputElement).value.trim() || null,
        label: name,
        auto_provision: false,
      };
    }
    const authType = (document.getElementById("srv-auth") as HTMLSelectElement).value;
    const body: Record<string, unknown> = {
      target_kind: "ssh",
      host: (document.getElementById("srv-host") as HTMLInputElement).value,
      ssh_port: Number((document.getElementById("srv-port") as HTMLInputElement).value || 22),
      ssh_user: (document.getElementById("srv-user") as HTMLInputElement).value,
      auth_type: authType,
      label: "primary",
    };
    if (authType === "key") body.ssh_private_key = (document.getElementById("srv-secret") as HTMLTextAreaElement).value;
    else body.ssh_password = (document.getElementById("srv-secret") as HTMLTextAreaElement).value;
    return body;
  };

  document.getElementById("test-ssh")?.addEventListener("click", async () => {
    if (!currentProjectId) {
      write({ error: "Выберите или создайте проект" });
      return;
    }
    const body = buildTargetBody();
    const resp = await fetch(`${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/targets/test-connection`, {
      method: "POST",
      headers: { ...authHeader(token()), "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    write(await resp.json());
  });

  document.getElementById("ssh-exec-run")?.addEventListener("click", async () => {
    if (!currentProjectId) {
      write({ error: "Выберите проект" });
      return;
    }
    const tid = (document.getElementById("ssh-exec-target") as HTMLSelectElement).value;
    const cmd = (document.getElementById("ssh-exec-cmd") as HTMLTextAreaElement).value;
    if (!tid || !cmd.trim()) {
      write({ error: "Выберите SSH-сервер и введите команду" });
      return;
    }
    const tokEl = document.getElementById("ssh-exec-token") as HTMLInputElement | null;
    const confirmed = (document.getElementById("ssh-exec-confirmed") as HTMLInputElement).checked;
    const outEl = document.getElementById("ssh-exec-out");
    const body: Record<string, unknown> = {
      command: cmd,
      confirmed,
    };
    const t = tokEl?.value?.trim();
    if (t) body.confirmation_token = t;
    try {
      const resp = await fetch(
        `${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/targets/${encodeURIComponent(tid)}/ssh-exec`,
        {
          method: "POST",
          headers: { ...authHeader(token()), "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
      );
      const data = (await resp.json()) as Record<string, unknown>;
      if (outEl) outEl.textContent = JSON.stringify(data, null, 2);
      write(data);
      if (data.needs_confirmation === true && typeof data.confirmation_token === "string" && tokEl) {
        tokEl.value = data.confirmation_token as string;
      }
      if (data.needs_confirmation !== true && tokEl) {
        tokEl.value = "";
        const c = document.getElementById("ssh-exec-confirmed") as HTMLInputElement | null;
        if (c) c.checked = false;
      }
    } catch (e) {
      write({ error: String(e) });
    }
  });

  document.getElementById("register-target")?.addEventListener("click", async () => {
    if (!currentProjectId) {
      write({ error: "Выберите или создайте проект" });
      return;
    }
    const kind = (document.getElementById("srv-kind") as HTMLSelectElement | null)?.value || "ssh";
    const body = buildTargetBody();
    body.auto_provision = kind !== "kubernetes";
    const resp = await fetch(`${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/targets`, {
      method: "POST",
      headers: { ...authHeader(token()), "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = (await resp.json()) as { target?: { id?: string }; provision?: { ok?: boolean; detail?: string } };
    if (data.target?.id) currentTargetId = data.target.id;
    write(data);
    void refreshWorkspaceHint();
    void loadTargetsList();
  });

  document.getElementById("prepare-ansible")?.addEventListener("click", async () => {
    if (!currentProjectId || !currentTargetId) {
      write({ error: "Сначала зарегистрируйте сервер" });
      return;
    }
    const resp = await fetch(
      `${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/targets/${currentTargetId}/prepare`,
      {
        method: "POST",
        headers: { ...authHeader(token()), "Content-Type": "application/json" },
        body: JSON.stringify({ template_id: "install-platform-agent" }),
      },
    );
    write(await resp.json());
  });

  document.getElementById("confirm-setup")?.addEventListener("click", async () => {
    if (!currentProjectId || !currentTargetId) {
      write({ error: "Нет target" });
      return;
    }
    const resp = await fetch(
      `${GATEWAY_BASE}/api/v1/projects/${currentProjectId}/targets/${currentTargetId}/confirm-setup`,
      { method: "POST", headers: authHeader(token()) },
    );
    write(await resp.json());
    void refreshWorkspaceHint();
  });

  if (statusPoll) window.clearInterval(statusPoll);
  statusPoll = window.setInterval(() => {
    if (currentProjectId) void refreshStatus();
  }, 8000);

  (window as unknown as { __lpPortalMounted?: boolean }).__lpPortalMounted = true;
  bindPortalNav();
  syncPortalView(portalViewFromRoute(route()));
}

function renderSuperadminRunsLive(runs: AiTeamLiveRun[]): void {
  const tbody = document.getElementById("ai-team-runs-live-body");
  if (!tbody) return;
  if (!runs.length) {
    tbody.innerHTML = `<tr><td colspan="8" class="lp-muted">Нет активных или недавних run</td></tr>`;
    return;
  }
  tbody.innerHTML = runs
    .map((r) => {
      const st = escapeHtml(r.status || "—");
      const phase = escapeHtml(String(r.phase || "—"));
      const iter = Number(r.iteration || 0);
      const b = escapeHtml(String(r.batch_status || "idle"));
      const updated = r.updated_at ? new Date(r.updated_at).toLocaleTimeString() : "—";
      const name = escapeHtml(r.project_name || "—");
      const pid = escapeHtml(r.project_id || "—");
      const runId = escapeHtml(r.run_id || "—");
      const err = r.error ? `<span class="lp-muted" title="${escapeHtml(String(r.error))}">⚠</span>` : "";
      return `<tr>
        <td><code>${runId}</code></td>
        <td title="${pid}">${name}</td>
        <td><span class="lp-badge">${st}</span>${err}</td>
        <td><code>${phase}</code></td>
        <td>${iter}</td>
        <td><code>${b}</code></td>
        <td>${escapeHtml(updated)}</td>
        <td><button type="button" class="lp-btn lp-btn-ghost" style="padding:4px 8px;font-size:12px;" data-superadmin-open-project="${pid}">Открыть</button></td>
      </tr>`;
    })
    .join("");
}

async function loadSuperadminRunsLive(): Promise<void> {
  const token =
    (document.getElementById("superadmin-token") as HTMLInputElement | null)?.value.trim() ||
    getSuperadminTokenForUi();
  if (!token) return;
  const status = (document.getElementById("ai-team-live-status") as HTMLSelectElement | null)?.value.trim() || "";
  const params = new URLSearchParams();
  params.set("limit", "80");
  if (status) params.set("status", status);
  const resp = await fetch(`${GATEWAY_BASE}/api/v1/admin/ai-team/runs/live?${params.toString()}`, {
    headers: authHeader(token),
  });
  if (!resp.ok) {
    const tbody = document.getElementById("ai-team-runs-live-body");
    if (tbody) tbody.innerHTML = `<tr><td colspan="8" class="lp-muted">ai-team/runs/live: ${resp.status}</td></tr>`;
    return;
  }
  const data = (await resp.json()) as { runs?: AiTeamLiveRun[] };
  renderSuperadminRunsLive(Array.isArray(data.runs) ? data.runs : []);
}

async function loadSuperadminData(): Promise<void> {
  const token =
    (document.getElementById("superadmin-token") as HTMLInputElement | null)?.value.trim() ||
    getSuperadminTokenForUi();
  if (token) localStorage.setItem(LS_SUPERADMIN_TOKEN, token);
  const filters = {
    search: (document.getElementById("filter-search") as HTMLInputElement | null)?.value.trim() || "",
    status: (document.getElementById("filter-status") as HTMLSelectElement | null)?.value.trim() || "",
    tenant_id: (document.getElementById("filter-tenant") as HTMLInputElement | null)?.value.trim() || "",
  };
  const params = new URLSearchParams();
  Object.entries(filters).forEach(([k, v]) => {
    if (v) params.set(k, v);
  });
  const [
    overviewResp,
    projectsResp,
    healthResp,
    metricsResp,
    llmResp,
    agentResp,
    usersResp,
    promptsResp,
    agToolsResp,
  ] = await Promise.all([
    fetch(`${GATEWAY_BASE}/api/v1/admin/overview`, { headers: authHeader(token) }),
    fetch(`${GATEWAY_BASE}/api/v1/admin/projects?${params.toString()}`, { headers: authHeader(token) }),
    fetch(`${GATEWAY_BASE}/api/v1/admin/services/health`, { headers: authHeader(token) }),
    fetch(`${GATEWAY_BASE}/metrics`),
    fetch(`${GATEWAY_BASE}/api/v1/admin/llm-settings`, { headers: authHeader(token) }),
    fetch(`${GATEWAY_BASE}/api/v1/admin/agent-settings`, { headers: authHeader(token) }),
    fetch(`${GATEWAY_BASE}/api/v1/admin/users`, { headers: authHeader(token) }),
    fetch(`${GATEWAY_BASE}/api/v1/admin/agent-development-prompts`, { headers: authHeader(token) }),
    fetch(`${GATEWAY_BASE}/api/v1/admin/agent-graph-tools`, { headers: authHeader(token) }),
  ]);
  const overview = await overviewResp.json();
  const projects = (await projectsResp.json()) as ProjectRow[];
  lastAdminProjectsSnapshot = projects;
  const health = await healthResp.json();
  const metricsText = await metricsResp.text();

  const byStatus = overview.projects_by_status || {};
  const setText = (id: string, value: string | number) => {
    const node = document.getElementById(id);
    if (node) node.textContent = String(value);
  };
  setText("kpi-total", overview.projects_total ?? 0);
  setText("kpi-plan", byStatus.PLAN_READY ?? 0);
  setText("kpi-infra", byStatus.INFRA_IN_PROGRESS ?? 0);
  setText("kpi-deployed", byStatus.DEPLOYED ?? 0);

  const tbody = document.getElementById("projects-table-body");
  if (tbody) {
    tbody.innerHTML = projects
      .map(
        (row) => `
      <tr>
        <td>${escapeHtml(row.name)}</td>
        <td class="lp-muted" style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeHtml(row.description || "")}">${escapeHtml((row.description || "").slice(0, 80))}${(row.description || "").length > 80 ? "…" : ""}</td>
        <td class="lp-muted">${escapeHtml(row.tenant_id)}</td>
        <td><span class="lp-badge">${escapeHtml(row.status)}</span></td>
        <td>${row.suspended ? '<span class="lp-badge" style="background:#fef3c7;color:#92400e">Пауза</span>' : "—"}</td>
        <td>${row.deleted_at ? `<span class="lp-badge" style="background:#fee2e2;color:#991b1b">Удалён</span>` : "—"}</td>
        <td>${escapeHtml(new Date(row.created_at).toLocaleString())}</td>
        <td style="white-space:nowrap;">
          <button type="button" class="lp-btn lp-btn-ghost" style="padding:4px 8px;font-size:12px" data-pa="edit" data-pid="${escapeHtml(row.id)}">Править</button>
          ${
            row.deleted_at
              ? `<button type="button" class="lp-btn lp-btn-ghost" style="padding:4px 8px;font-size:12px" data-pa="restore" data-pid="${escapeHtml(row.id)}">Восстановить</button>`
              : `<button type="button" class="lp-btn lp-btn-ghost" style="padding:4px 8px;font-size:12px" data-pa="${row.suspended ? "resume" : "suspend"}" data-pid="${escapeHtml(row.id)}">${row.suspended ? "Снять паузу" : "Пауза"}</button>
                 <button type="button" class="lp-btn lp-btn-ghost" style="padding:4px 8px;font-size:12px;color:#b91c1c" data-pa="delete" data-pid="${escapeHtml(row.id)}">Удалить</button>`
          }
        </td>
      </tr>
    `,
      )
      .join("");
  }

  const usersTbody = document.getElementById("users-table-body");
  if (usersTbody) {
    if (usersResp.ok) {
      const users = (await usersResp.json()) as { email: string; tenant_id: string; created_at: string }[];
      usersTbody.innerHTML = users
        .map(
          (u) => `
      <tr>
        <td>${escapeHtml(u.email)}</td>
        <td class="lp-muted"><code>${escapeHtml(u.tenant_id)}</code></td>
        <td>${escapeHtml(new Date(u.created_at).toLocaleString())}</td>
      </tr>`,
        )
        .join("");
    } else {
      usersTbody.innerHTML = `<tr><td colspan="3" class="lp-muted">users: ${usersResp.status}</td></tr>`;
    }
  }

  const healthEl = document.getElementById("services-health");
  if (healthEl) {
    const svc = (health.services ?? {}) as Record<string, string>;
    healthEl.innerHTML = `<div class="lp-health-grid">${Object.entries(svc)
      .map(([name, st]) => {
        const ok = st === "ok";
        return `<div class="lp-health-pill ${ok ? "ok" : "bad"}"><span>${escapeHtml(name)}</span><span>${escapeHtml(st)}</span></div>`;
      })
      .join("")}</div>`;
  }
  const metricsNode = document.getElementById("metrics-output");
  if (metricsNode) metricsNode.textContent = metricsText;

  const baseUrlEl = document.getElementById("llm-base-url") as HTMLInputElement | null;
  const modelEl = document.getElementById("llm-default-model") as HTMLInputElement | null;
  const codingEl = document.getElementById("llm-coding-model") as HTMLInputElement | null;
  const embeddingEl = document.getElementById("llm-embedding-model") as HTMLInputElement | null;
  const keyStatusEl = document.getElementById("llm-key-status");
  if (llmResp.ok && baseUrlEl && modelEl) {
    const llm = (await llmResp.json()) as {
      openai_base_url: string;
      default_model: string;
      coding_model: string | null;
      embedding_model: string | null;
      has_api_key: boolean;
      api_key_hint: string | null;
    };
    baseUrlEl.value = llm.openai_base_url ?? "";
    modelEl.value = llm.default_model ?? "";
    if (codingEl) codingEl.value = llm.coding_model ?? "";
    if (embeddingEl) embeddingEl.value = llm.embedding_model ?? "";
    if (keyStatusEl) {
      keyStatusEl.textContent = llm.has_api_key
        ? `(ключ задан${llm.api_key_hint ? ` ${llm.api_key_hint}` : ""})`
        : "(ключ не задан — используется mock без OPENAI_API_KEY в env)";
    }
  }

  const agentIngestEl = document.getElementById("agent-ingest-url") as HTMLInputElement | null;
  if (agentResp.ok && agentIngestEl) {
    const agent = (await agentResp.json()) as { ingest_public_url: string };
    agentIngestEl.value = agent.ingest_public_url ?? "";
  }

  const ppm = document.getElementById("prompt-pm") as HTMLTextAreaElement | null;
  const pa = document.getElementById("prompt-architect") as HTMLTextAreaElement | null;
  const pd = document.getElementById("prompt-devops") as HTMLTextAreaElement | null;
  const pb = document.getElementById("prompt-backend") as HTMLTextAreaElement | null;
  const pf = document.getElementById("prompt-frontend") as HTMLTextAreaElement | null;
  if (promptsResp.ok && pd && pb && pf) {
    const pr = (await promptsResp.json()) as { prompts?: Record<string, string> };
    const p = pr.prompts || {};
    if (ppm) ppm.value = p.pm ?? "";
    if (pa) pa.value = p.architect ?? "";
    pd.value = p.devops ?? "";
    pb.value = p.backend ?? "";
    pf.value = p.frontend ?? "";
  }

  const agWrap = document.getElementById("agent-graph-builtin-wrap");
  const agHttpBody = document.getElementById("agent-graph-http-body");
  if (agWrap && agHttpBody) {
    if (!agToolsResp.ok) {
      const err = `agent-graph-tools: ${agToolsResp.status}`;
      agWrap.innerHTML = `<p class="lp-error">${escapeHtml(err)}</p>`;
      agHttpBody.innerHTML = `<tr><td colspan="5" class="lp-muted">${escapeHtml(err)}</td></tr>`;
    } else {
      const ag = (await agToolsResp.json()) as {
        builtins: { tool_key: string; label: string; enabled: boolean }[];
        http_tools: {
          id: string;
          tool_name: string;
          description: string;
          url_template: string;
          enabled: boolean;
        }[];
      };
      agWrap.innerHTML = (ag.builtins || [])
        .map(
          (b) => `
    <label class="lp-field" style="display:block;margin-bottom:6px;">
      <input type="checkbox" data-ag-builtin="${escapeHtml(b.tool_key)}" ${b.enabled ? "checked" : ""} />
      ${escapeHtml(b.label || b.tool_key)} <code class="lp-muted">${escapeHtml(b.tool_key)}</code>
    </label>`,
        )
        .join("");
      if (!(ag.builtins || []).length) {
        agWrap.innerHTML = `<p class="lp-muted">Нет записей встроенных tools</p>`;
      }
      const rows = ag.http_tools || [];
      agHttpBody.innerHTML = rows.length
        ? rows
            .map(
              (h) => `
    <tr>
      <td><code>${escapeHtml(h.tool_name)}</code></td>
      <td class="lp-muted" style="max-width:200px;overflow:hidden;text-overflow:ellipsis" title="${escapeHtml(h.description)}">${escapeHtml(h.description)}</td>
      <td class="lp-muted" style="max-width:240px;overflow:hidden;text-overflow:ellipsis" title="${escapeHtml(h.url_template)}"><code>${escapeHtml(h.url_template)}</code></td>
      <td><input type="checkbox" data-ag-http-en="${escapeHtml(h.id)}" ${h.enabled ? "checked" : ""} /></td>
      <td><button type="button" class="lp-btn lp-btn-ghost" style="padding:4px 8px;color:#b91c1c" data-ag-http-del="${escapeHtml(h.id)}">Удалить</button></td>
    </tr>`,
            )
            .join("")
        : `<tr><td colspan="5" class="lp-muted">Нет HTTP tools</td></tr>`;
    }
  }
  await loadSuperadminRunsLive();
}

function setupAnsibleTemplates(): void {
  const token = () =>
    (document.getElementById("superadmin-token") as HTMLInputElement)?.value.trim() || getSuperadminTokenForUi();
  const auth = () => authHeader(token());

  let pair: AnsibleSwarmEditorPair | null = null;
  let editingNew = false;

  const metaHost = () => document.getElementById("ansible-meta-cm");
  const playHost = () => document.getElementById("ansible-play-cm");
  const wrap = document.getElementById("ansible-editor-wrap");
  const idInput = document.getElementById("ansible-edit-id") as HTMLInputElement | null;
  const previewOut = document.getElementById("ansible-preview-out");

  function destroyEditors(): void {
    if (pair) {
      pair.destroy();
      pair = null;
    }
  }

  function ensureEditors(meta: string, play: string): void {
    const mh = metaHost();
    const ph = playHost();
    if (!mh || !ph) return;
    destroyEditors();
    pair = mountAnsibleSwarmEditors(mh, ph, meta, play);
  }

  const setEditor = (meta: string, play: string) => {
    if (pair) {
      setEditorContent(pair.meta, meta);
      setEditorContent(pair.play, play);
    } else {
      ensureEditors(meta, play);
    }
  };

  const getMeta = () => (pair ? getEditorContent(pair.meta) : "");
  const getPlay = () => (pair ? getEditorContent(pair.play) : "");

  async function loadList(): Promise<void> {
    const tbody = document.getElementById("ansible-table-body");
    if (!tbody) return;
    tbody.innerHTML = `<tr><td colspan="4" class="lp-muted">Loading…</td></tr>`;
    const resp = await fetch(`${GATEWAY_BASE}/api/v1/admin/ansible/swarm/templates`, { headers: auth() });
    if (!resp.ok) {
      tbody.innerHTML = `<tr><td colspan="4" class="lp-error">${escapeHtml(await resp.text())}</td></tr>`;
      return;
    }
    const rows = (await resp.json()) as { id: string; name: string; active: boolean }[];
    tbody.innerHTML = rows
      .map(
        (r) => `
      <tr data-id="${escapeHtml(r.id)}">
        <td><code>${escapeHtml(r.id)}</code></td>
        <td>${escapeHtml(r.name)}</td>
        <td>${r.active ? `<span class="lp-badge">active</span>` : `<span class="lp-muted">off</span>`}</td>
        <td style="white-space:nowrap;">
          <button type="button" class="lp-btn lp-btn-ghost ansible-edit" data-id="${escapeHtml(r.id)}">Edit</button>
          <button type="button" class="lp-btn lp-btn-ghost ansible-toggle" data-id="${escapeHtml(r.id)}" data-active="${r.active}">${r.active ? "Deactivate" : "Activate"}</button>
        </td>
      </tr>`,
      )
      .join("");

    tbody.querySelectorAll(".ansible-edit").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const id = (btn as HTMLButtonElement).dataset.id;
        if (!id) return;
        editingNew = false;
        if (idInput) {
          idInput.value = id;
          idInput.readOnly = true;
        }
        const d = await fetch(`${GATEWAY_BASE}/api/v1/admin/ansible/swarm/templates/${encodeURIComponent(id)}`, {
          headers: auth(),
        });
        if (!d.ok) {
          alert(await d.text());
          return;
        }
        const t = (await d.json()) as { meta_yaml: string; playbook_j2: string };
        setEditor(t.meta_yaml, t.playbook_j2);
        if (wrap) wrap.style.display = "block";
        if (previewOut) previewOut.style.display = "none";
      });
    });

    tbody.querySelectorAll(".ansible-toggle").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const id = (btn as HTMLButtonElement).dataset.id;
        const active = (btn as HTMLButtonElement).dataset.active === "true";
        if (!id) return;
        const resp = await fetch(
          `${GATEWAY_BASE}/api/v1/admin/ansible/swarm/templates/${encodeURIComponent(id)}/active`,
          {
            method: "PATCH",
            headers: { ...auth(), "Content-Type": "application/json" },
            body: JSON.stringify({ active: !active }),
          },
        );
        if (!resp.ok) {
          alert(await resp.text());
          return;
        }
        void loadList();
      });
    });
  }

  document.getElementById("ansible-load-list")?.addEventListener("click", () => void loadList());
  document.getElementById("ansible-new")?.addEventListener("click", () => {
    editingNew = true;
    if (idInput) {
      idInput.value = "my-template";
      idInput.readOnly = false;
    }
    setEditor(
      `id: my-template\nactive: true\nname: New template\ndescription: ""\ntags: []\nvars_schema: {}\n`,
      `---\n- hosts: all\n  become: true\n  tasks:\n    - name: Example\n      debug:\n        msg: "Edit playbook"\n`,
    );
    if (wrap) wrap.style.display = "block";
    if (previewOut) previewOut.style.display = "none";
  });

  document.getElementById("ansible-save")?.addEventListener("click", async () => {
    const metaYaml = getMeta();
    const playbookJ2 = getPlay();
    const tid = (idInput?.value ?? "").trim();
    if (!tid) {
      alert("Template id required");
      return;
    }
    if (editingNew) {
      const resp = await fetch(`${GATEWAY_BASE}/api/v1/admin/ansible/swarm/templates`, {
        method: "POST",
        headers: { ...auth(), "Content-Type": "application/json" },
        body: JSON.stringify({ id: tid, meta_yaml: metaYaml, playbook_j2: playbookJ2 }),
      });
      if (!resp.ok) {
        alert(await resp.text());
        return;
      }
      editingNew = false;
      if (idInput) idInput.readOnly = true;
    } else {
      const resp = await fetch(`${GATEWAY_BASE}/api/v1/admin/ansible/swarm/templates/${encodeURIComponent(tid)}`, {
        method: "PUT",
        headers: { ...auth(), "Content-Type": "application/json" },
        body: JSON.stringify({ meta_yaml: metaYaml, playbook_j2: playbookJ2 }),
      });
      if (!resp.ok) {
        alert(await resp.text());
        return;
      }
    }
    void loadList();
  });

  document.getElementById("ansible-preview")?.addEventListener("click", async () => {
    const tid = (idInput?.value ?? "").trim();
    if (!tid || editingNew) {
      alert("Save the template first, then preview.");
      return;
    }
    const resp = await fetch(`${GATEWAY_BASE}/api/v1/admin/ansible/swarm/templates/${encodeURIComponent(tid)}/preview`, {
      method: "POST",
      headers: { ...auth(), "Content-Type": "application/json" },
      body: JSON.stringify({ variables: {} }),
    });
    const data = await resp.json();
    if (previewOut) {
      previewOut.style.display = "block";
      previewOut.textContent = resp.ok ? (data as { playbook_yaml?: string }).playbook_yaml ?? JSON.stringify(data, null, 2) : JSON.stringify(data, null, 2);
    }
  });

  document.getElementById("ansible-delete")?.addEventListener("click", async () => {
    const tid = (idInput?.value ?? "").trim();
    if (!tid || editingNew) return;
    if (!confirm(`Delete template ${tid}?`)) return;
    const resp = await fetch(`${GATEWAY_BASE}/api/v1/admin/ansible/swarm/templates/${encodeURIComponent(tid)}`, {
      method: "DELETE",
      headers: auth(),
    });
    if (!resp.ok) {
      alert(await resp.text());
      return;
    }
    if (wrap) wrap.style.display = "none";
    destroyEditors();
    void loadList();
  });

  void loadList();
}

function renderSuperadmin(): void {
  if (superadminPhasePollTimer !== null) {
    window.clearInterval(superadminPhasePollTimer);
    superadminPhasePollTimer = null;
  }
  mount(
    shell({
      active: "superadmin",
      breadcrumb: `Home <span>/</span> Superadmin`,
      title: "Superadmin",
      children: `
      <div class="lp-panel">
        <div class="lp-row">
          <div class="lp-field"><label>Superadmin token</label>
            <input class="lp-input" id="superadmin-token" type="password" autocomplete="off" value="${escapeHtml(getSuperadminTokenForUi())}" style="max-width:420px;" />
          </div>
          <button type="button" class="lp-btn" id="refresh-all">Refresh</button>
        </div>
      </div>
      <div class="lp-panel">
        <h2>Portal users</h2>
        <p class="lp-muted">Создайте учётную запись для демо. У каждого пользователя свой tenant; вход: <code>#/login</code>.</p>
        <div class="lp-row" style="flex-wrap:wrap;gap:8px;align-items:flex-end;">
          <div class="lp-field"><label>Email</label><input class="lp-input" id="new-user-email" type="email" placeholder="owner@company.com" style="min-width:220px" /></div>
          <div class="lp-field"><label>Password</label><input class="lp-input" id="new-user-password" type="password" style="min-width:160px" /></div>
          <div class="lp-field"><label>Имя</label><input class="lp-input" id="new-user-name" placeholder="Owner" style="min-width:140px" /></div>
          <button type="button" class="lp-btn" id="create-user-btn">Создать пользователя</button>
        </div>
        <div class="lp-table-wrap" style="margin-top:12px">
          <table class="lp-table">
            <thead><tr><th>Email</th><th>Tenant</th><th>Создан</th></tr></thead>
            <tbody id="users-table-body"></tbody>
          </table>
        </div>
      </div>
      <div class="lp-panel">
        <h2>LLM adapter</h2>
        <p class="lp-muted">OpenAI-compatible API. Значения хранятся в admin DB; ключ шифруется (Fernet). Переопределяют env в контейнере llm-adapter-service. Пустые поля «код» / «эмбеддинги» — использовать модель по умолчанию (или env <code>OPENAI_CODING_MODEL</code> / <code>OPENAI_EMBEDDING_MODEL</code>).</p>
        <div class="lp-row">
          <div class="lp-field">
            <label>Base URL</label>
            <input class="lp-input" id="llm-base-url" placeholder="https://api.openai.com/v1" style="min-width: 320px" />
          </div>
          <div class="lp-field">
            <label>Модель по умолчанию (общие задачи)</label>
            <input class="lp-input" id="llm-default-model" placeholder="gpt-4o-mini" style="min-width: 220px" />
          </div>
        </div>
        <div class="lp-row">
          <div class="lp-field">
            <label>Модель для кода / агентов</label>
            <input class="lp-input" id="llm-coding-model" placeholder="Оставьте пустым = как выше" style="min-width: 220px" />
          </div>
          <div class="lp-field">
            <label>Модель эмбеддингов</label>
            <input class="lp-input" id="llm-embedding-model" placeholder="text-embedding-3-small" style="min-width: 220px" />
          </div>
        </div>
        <div class="lp-field">
          <label>API key <span id="llm-key-status" class="lp-muted"></span></label>
          <input class="lp-input" type="password" id="llm-api-key" placeholder="Пусто = не менять ключ" autocomplete="off" style="max-width: 480px" />
        </div>
        <button type="button" class="lp-btn" id="llm-settings-save">Save LLM settings</button>
      </div>
      <div class="lp-panel">
        <h2>Remote agent (ingest)</h2>
        <p class="lp-muted">Полный URL <code>POST .../api/v1/agent/ingest</code>, доступный с серверов пользователей (интернет или VPN). Сохраняется в admin DB; adapter подставляет его при установке агента. Fallback — переменная <code>AGENT_INGEST_PUBLIC_URL</code> в контейнере adapter.</p>
        <div class="lp-field">
          <label>Ingest URL</label>
          <input class="lp-input" id="agent-ingest-url" placeholder="https://your-domain/api/v1/agent/ingest" style="max-width: 560px" />
        </div>
        <button type="button" class="lp-btn" id="agent-settings-save">Сохранить URL агента</button>
      </div>
      <div class="lp-panel">
        <h2>Агенты разработки (системные промпты)</h2>
        <p class="lp-muted">Роли PM, архитектор, DevOps, backend и frontend — базовый контекст для задач и демо агентов (см. development-context).</p>
        <div class="lp-field"><label>PM</label><textarea class="lp-textarea" id="prompt-pm" rows="3" style="width:100%;max-width:720px;"></textarea></div>
        <div class="lp-field"><label>Architect</label><textarea class="lp-textarea" id="prompt-architect" rows="3" style="width:100%;max-width:720px;"></textarea></div>
        <div class="lp-field"><label>DevOps</label><textarea class="lp-textarea" id="prompt-devops" rows="4" style="width:100%;max-width:720px;"></textarea></div>
        <div class="lp-field"><label>Backend</label><textarea class="lp-textarea" id="prompt-backend" rows="4" style="width:100%;max-width:720px;"></textarea></div>
        <div class="lp-field"><label>Frontend</label><textarea class="lp-textarea" id="prompt-frontend" rows="4" style="width:100%;max-width:720px;"></textarea></div>
        <button type="button" class="lp-btn" id="agent-dev-prompts-save">Сохранить промпты</button>
      </div>
      <div class="lp-panel" id="agent-graph-tools-panel">
        <h2>LangGraph agent tools</h2>
        <p class="lp-muted">Встроенные инструменты можно отключить. HTTP GET: URL с <code>{project_id}</code>, хосты <code>adapter-service</code> и <code>project-service</code>, путь <code>/api/v1/projects/...</code>.</p>
        <h3 style="font-size:14px;margin:12px 0 8px;">Встроенные</h3>
        <div id="agent-graph-builtin-wrap" class="lp-muted">Загрузка…</div>
        <button type="button" class="lp-btn" id="agent-graph-builtin-save" style="margin-top:8px;">Сохранить встроенные</button>
        <h3 style="font-size:14px;margin:16px 0 8px;">HTTP tools (GET)</h3>
        <div id="agent-graph-http-panel" class="lp-table-wrap">
          <table class="lp-table">
            <thead><tr><th>Имя</th><th>Описание</th><th>URL шаблон</th><th>Вкл</th><th></th></tr></thead>
            <tbody id="agent-graph-http-body"><tr><td colspan="5" class="lp-muted">—</td></tr></tbody>
          </table>
        </div>
        <div class="lp-row" style="flex-wrap:wrap;gap:8px;align-items:flex-end;margin-top:12px;">
          <div class="lp-field"><label>tool_name</label><input class="lp-input" id="ag-http-name" placeholder="my_project_facts" style="min-width:180px" /></div>
          <div class="lp-field"><label>Описание</label><input class="lp-input" id="ag-http-desc" placeholder="Кратко для LLM" style="min-width:220px" /></div>
          <div class="lp-field" style="flex:1;min-width:280px;"><label>url_template</label><input class="lp-input" id="ag-http-url" placeholder="http://project-service:8002/api/v1/projects/{project_id}/facts" style="width:100%;max-width:480px" /></div>
          <button type="button" class="lp-btn" id="agent-graph-http-add">Добавить</button>
        </div>
      </div>
      <div class="lp-panel">
        <h2>AI Team phase dashboard (live)</h2>
        <p class="lp-muted">Обновляется автоматически каждые 3с. Показывает текущую фазу, iteration батча и состояние исполнения.</p>
        <div class="lp-row" style="align-items:flex-end;gap:8px;">
          <div class="lp-field">
            <label>Фильтр статуса run</label>
            <select id="ai-team-live-status" class="lp-select" style="min-width:180px;">
              <option value="">Все</option>
              <option value="running">running</option>
              <option value="paused">paused</option>
              <option value="error">error</option>
              <option value="done">done</option>
            </select>
          </div>
          <button type="button" class="lp-btn lp-btn-ghost" id="ai-team-live-refresh">Обновить сейчас</button>
        </div>
        <div class="lp-table-wrap" style="margin-top:12px;">
          <table class="lp-table">
            <thead>
              <tr>
                <th>Run ID</th>
                <th>Project</th>
                <th>Status</th>
                <th>Phase</th>
                <th>Iteration</th>
                <th>Batch status</th>
                <th>Updated</th>
                <th></th>
              </tr>
            </thead>
            <tbody id="ai-team-runs-live-body"><tr><td colspan="8" class="lp-muted">Загрузка…</td></tr></tbody>
          </table>
        </div>
      </div>
      <div class="lp-stat-grid">
        <article class="lp-stat-card">
          <div><h3>Total projects</h3><div class="lp-big" id="kpi-total">—</div></div>
        </article>
        <article class="lp-stat-card">
          <div><h3>Plan ready</h3><div class="lp-big" id="kpi-plan">—</div></div>
        </article>
        <article class="lp-stat-card">
          <div><h3>Infra in progress</h3><div class="lp-big" id="kpi-infra">—</div></div>
        </article>
        <article class="lp-stat-card">
          <div><h3>Deployed</h3><div class="lp-big" id="kpi-deployed">—</div></div>
        </article>
      </div>
      <div class="lp-panel">
        <h2>Filters</h2>
        <div class="lp-row">
          <div class="lp-field"><label>Search</label><input class="lp-input" id="filter-search" placeholder="Project name" /></div>
          <div class="lp-field"><label>Tenant UUID</label><input class="lp-input" id="filter-tenant" placeholder="Tenant UUID" style="min-width:260px;" /></div>
          <div class="lp-field"><label>Status</label>
            <select class="lp-select" id="filter-status" style="min-width:180px;">
              <option value="">All statuses</option>
              <option>IDEA</option><option>PLAN_READY</option><option>INFRA_IN_PROGRESS</option>
              <option>INFRA_READY</option><option>CODE_IN_PROGRESS</option><option>CODE_READY</option>
              <option>DEPLOY_IN_PROGRESS</option><option>DEPLOYED</option><option>FAILED</option>
            </select>
          </div>
          <button type="button" class="lp-btn lp-btn-ghost" id="apply-filters">Apply filters</button>
        </div>
      </div>
      <div class="lp-panel">
        <h2>Проекты (управление)</h2>
        <p class="lp-muted">Пауза блокирует изменения и LLM для пользователей; удаление — мягкое (скрыто из списков tenant).</p>
        <div class="lp-table-wrap">
          <table class="lp-table">
            <thead>
              <tr>
                <th>Имя</th>
                <th>Описание</th>
                <th>Tenant</th>
                <th>Статус workflow</th>
                <th>Пауза</th>
                <th>Удалён</th>
                <th>Обновлён</th>
                <th>Действия</th>
              </tr>
            </thead>
            <tbody id="projects-table-body"></tbody>
          </table>
        </div>
      </div>
      <div class="lp-panel" id="ansible-templates-panel">
        <h2>Ansible swarm templates</h2>
        <p class="lp-muted">Хранение в S3 (RustFS). В пользовательском каталоге только шаблоны с <code>active: true</code> в <code>meta.yaml</code>.</p>
        <div class="lp-row">
          <button type="button" class="lp-btn lp-btn-ghost" id="ansible-load-list">Reload list</button>
          <button type="button" class="lp-btn" id="ansible-new">New template</button>
        </div>
        <div class="lp-table-wrap" style="margin-top: 12px">
          <table class="lp-table">
            <thead>
              <tr>
                <th>ID</th>
                <th>Name</th>
                <th>Active</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody id="ansible-table-body"></tbody>
          </table>
        </div>
        <div id="ansible-editor-wrap" style="display: none; margin-top: 16px">
          <div class="lp-row">
            <div class="lp-field" style="flex: 1">
              <label>Template id</label>
              <input class="lp-input" id="ansible-edit-id" readonly style="max-width: 360px" />
            </div>
          </div>
          <div class="lp-field">
            <label>meta.yaml</label>
            <div id="ansible-meta-cm" class="ansible-ide-editor" aria-label="meta.yaml"></div>
          </div>
          <div class="lp-field">
            <label>playbook.yml.j2 (Jinja2)</label>
            <div id="ansible-play-cm" class="ansible-ide-editor" aria-label="playbook.yml.j2"></div>
          </div>
          <p class="lp-muted" style="font-size: 12px; margin: 8px 0">
            Редактор Monaco (как в VS Code): подсветка YAML, миникарта, сворачивание, поиск и замена (Ctrl+H),
            мультикурсор (Alt+Click). Jinja <code>{{ }}</code> в playbook остаётся в рамках YAML-подсветки.
          </p>
          <div class="lp-row">
            <button type="button" class="lp-btn" id="ansible-save">Save</button>
            <button type="button" class="lp-btn lp-btn-ghost" id="ansible-preview">Preview render</button>
            <button type="button" class="lp-btn lp-btn-ghost" id="ansible-delete" style="color: #b91c1c">Delete</button>
          </div>
          <pre class="lp-pre" id="ansible-preview-out" style="display: none; margin-top: 12px"></pre>
        </div>
      </div>
      <div class="lp-panel">
        <h2>Services health</h2>
        <div id="services-health" class="lp-muted">Loading…</div>
      </div>
      <div class="lp-panel">
        <h2>Prometheus metrics <span class="lp-muted">(/metrics)</span></h2>
        <pre class="lp-pre" id="metrics-output"></pre>
      </div>
      `,
    }),
  );

  const safeLoad = () =>
    loadSuperadminData().catch((err) => {
      const healthNode = document.getElementById("services-health");
      if (healthNode) healthNode.innerHTML = `<p class="lp-error">${escapeHtml(String(err))}</p>`;
    });
  document.getElementById("refresh-all")?.addEventListener("click", safeLoad);
  document.getElementById("apply-filters")?.addEventListener("click", safeLoad);
  document.getElementById("ai-team-live-refresh")?.addEventListener("click", () => void loadSuperadminRunsLive());
  document.getElementById("ai-team-live-status")?.addEventListener("change", () => void loadSuperadminRunsLive());
  document.getElementById("ai-team-runs-live-body")?.addEventListener("click", (ev) => {
    const btn = (ev.target as HTMLElement).closest("button[data-superadmin-open-project]") as HTMLButtonElement | null;
    if (!btn) return;
    const pid = btn.getAttribute("data-superadmin-open-project");
    if (!pid) return;
    location.hash = `#/portal/project/${encodeURIComponent(pid)}`;
  });
  document.getElementById("projects-table-body")?.addEventListener("click", async (ev) => {
    const el = (ev.target as HTMLElement).closest("button[data-pa]");
    if (!el) return;
    const action = el.getAttribute("data-pa");
    const pid = el.getAttribute("data-pid");
    if (!action || !pid) return;
    const t =
      (document.getElementById("superadmin-token") as HTMLInputElement | null)?.value.trim() ||
      getSuperadminTokenForUi();
    if (!t) {
      alert("Нужен superadmin token");
      return;
    }
    const headers = { ...authHeader(t), "Content-Type": "application/json" };
    try {
      if (action === "edit") {
        const row = lastAdminProjectsSnapshot.find((p) => p.id === pid);
        const name = prompt("Имя проекта", row?.name ?? "");
        if (name === null) return;
        const desc = prompt("Описание", row?.description ?? "");
        if (desc === null) return;
        const resp = await fetch(`${GATEWAY_BASE}/api/v1/admin/projects/${encodeURIComponent(pid)}`, {
          method: "PATCH",
          headers,
          body: JSON.stringify({ name: name.trim(), description: desc }),
        });
        if (!resp.ok) alert(await resp.text());
      } else if (action === "suspend") {
        const resp = await fetch(`${GATEWAY_BASE}/api/v1/admin/projects/${encodeURIComponent(pid)}`, {
          method: "PATCH",
          headers,
          body: JSON.stringify({ suspended: true }),
        });
        if (!resp.ok) alert(await resp.text());
      } else if (action === "resume") {
        const resp = await fetch(`${GATEWAY_BASE}/api/v1/admin/projects/${encodeURIComponent(pid)}`, {
          method: "PATCH",
          headers,
          body: JSON.stringify({ suspended: false }),
        });
        if (!resp.ok) alert(await resp.text());
      } else if (action === "delete") {
        if (!confirm(`Удалить проект ${pid}? (мягкое удаление)`)) return;
        const resp = await fetch(`${GATEWAY_BASE}/api/v1/admin/projects/${encodeURIComponent(pid)}`, {
          method: "DELETE",
          headers: authHeader(t),
        });
        if (!resp.ok) alert(await resp.text());
      } else if (action === "restore") {
        const resp = await fetch(`${GATEWAY_BASE}/api/v1/admin/projects/${encodeURIComponent(pid)}`, {
          method: "PATCH",
          headers,
          body: JSON.stringify({ deleted: false }),
        });
        if (!resp.ok) alert(await resp.text());
      }
    } finally {
      await safeLoad();
    }
  });
  document.getElementById("llm-settings-save")?.addEventListener("click", async () => {
    const t = (document.getElementById("superadmin-token") as HTMLInputElement)?.value.trim() || getSuperadminTokenForUi();
    const body: Record<string, string | undefined> = {
      openai_base_url: (document.getElementById("llm-base-url") as HTMLInputElement).value.trim(),
      default_model: (document.getElementById("llm-default-model") as HTMLInputElement).value.trim(),
      coding_model: (document.getElementById("llm-coding-model") as HTMLInputElement).value.trim(),
      embedding_model: (document.getElementById("llm-embedding-model") as HTMLInputElement).value.trim(),
    };
    const newKey = (document.getElementById("llm-api-key") as HTMLInputElement).value;
    if (newKey.trim()) body.api_key = newKey.trim();
    const resp = await fetch(`${GATEWAY_BASE}/api/v1/admin/llm-settings`, {
      method: "PUT",
      headers: { ...authHeader(t), "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      alert(await resp.text());
      return;
    }
    (document.getElementById("llm-api-key") as HTMLInputElement).value = "";
    await safeLoad();
  });
  document.getElementById("agent-settings-save")?.addEventListener("click", async () => {
    const t = (document.getElementById("superadmin-token") as HTMLInputElement)?.value.trim() || getSuperadminTokenForUi();
    const ingest_public_url = (document.getElementById("agent-ingest-url") as HTMLInputElement).value.trim();
    if (!ingest_public_url) {
      alert("Укажите URL");
      return;
    }
    const resp = await fetch(`${GATEWAY_BASE}/api/v1/admin/agent-settings`, {
      method: "PUT",
      headers: { ...authHeader(t), "Content-Type": "application/json" },
      body: JSON.stringify({ ingest_public_url }),
    });
    if (!resp.ok) {
      alert(await resp.text());
      return;
    }
    await safeLoad();
  });
  document.getElementById("agent-dev-prompts-save")?.addEventListener("click", async () => {
    const t = (document.getElementById("superadmin-token") as HTMLInputElement)?.value.trim() || getSuperadminTokenForUi();
    const pm = (document.getElementById("prompt-pm") as HTMLTextAreaElement).value;
    const architect = (document.getElementById("prompt-architect") as HTMLTextAreaElement).value;
    const devops = (document.getElementById("prompt-devops") as HTMLTextAreaElement).value;
    const backend = (document.getElementById("prompt-backend") as HTMLTextAreaElement).value;
    const frontend = (document.getElementById("prompt-frontend") as HTMLTextAreaElement).value;
    const resp = await fetch(`${GATEWAY_BASE}/api/v1/admin/agent-development-prompts`, {
      method: "PUT",
      headers: { ...authHeader(t), "Content-Type": "application/json" },
      body: JSON.stringify({ prompts: { pm, architect, devops, backend, frontend } }),
    });
    if (!resp.ok) {
      alert(await resp.text());
      return;
    }
    await safeLoad();
  });
  document.getElementById("agent-graph-builtin-save")?.addEventListener("click", async () => {
    const t =
      (document.getElementById("superadmin-token") as HTMLInputElement)?.value.trim() || getSuperadminTokenForUi();
    const tools: Record<string, boolean> = {};
    document.querySelectorAll<HTMLInputElement>("[data-ag-builtin]").forEach((el) => {
      const k = el.getAttribute("data-ag-builtin");
      if (k) tools[k] = el.checked;
    });
    const resp = await fetch(`${GATEWAY_BASE}/api/v1/admin/agent-graph-tools/builtins`, {
      method: "PUT",
      headers: { ...authHeader(t), "Content-Type": "application/json" },
      body: JSON.stringify({ tools }),
    });
    if (!resp.ok) {
      alert(await resp.text());
      return;
    }
    await safeLoad();
  });
  document.getElementById("agent-graph-http-add")?.addEventListener("click", async () => {
    const t =
      (document.getElementById("superadmin-token") as HTMLInputElement)?.value.trim() || getSuperadminTokenForUi();
    const tool_name = (document.getElementById("ag-http-name") as HTMLInputElement).value.trim();
    const description = (document.getElementById("ag-http-desc") as HTMLInputElement).value.trim();
    const url_template = (document.getElementById("ag-http-url") as HTMLInputElement).value.trim();
    if (!tool_name || !description || !url_template) {
      alert("Заполните все поля");
      return;
    }
    const resp = await fetch(`${GATEWAY_BASE}/api/v1/admin/agent-graph-tools/http`, {
      method: "POST",
      headers: { ...authHeader(t), "Content-Type": "application/json" },
      body: JSON.stringify({ tool_name, description, url_template, enabled: true }),
    });
    if (!resp.ok) {
      alert(await resp.text());
      return;
    }
    (document.getElementById("ag-http-name") as HTMLInputElement).value = "";
    (document.getElementById("ag-http-desc") as HTMLInputElement).value = "";
    (document.getElementById("ag-http-url") as HTMLInputElement).value = "";
    await safeLoad();
  });
  document.getElementById("agent-graph-http-panel")?.addEventListener("change", async (ev) => {
    const inp = ev.target as HTMLInputElement;
    if (!inp.matches("[data-ag-http-en]")) return;
    const id = inp.getAttribute("data-ag-http-en");
    if (!id) return;
    const t =
      (document.getElementById("superadmin-token") as HTMLInputElement)?.value.trim() || getSuperadminTokenForUi();
    const resp = await fetch(`${GATEWAY_BASE}/api/v1/admin/agent-graph-tools/http/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: { ...authHeader(t), "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: inp.checked }),
    });
    if (!resp.ok) {
      alert(await resp.text());
      inp.checked = !inp.checked;
      return;
    }
  });
  document.getElementById("agent-graph-http-panel")?.addEventListener("click", async (ev) => {
    const btn = (ev.target as HTMLElement).closest("[data-ag-http-del]");
    if (!btn) return;
    const id = btn.getAttribute("data-ag-http-del");
    if (!id || !confirm("Удалить этот HTTP tool?")) return;
    const t =
      (document.getElementById("superadmin-token") as HTMLInputElement)?.value.trim() || getSuperadminTokenForUi();
    const resp = await fetch(`${GATEWAY_BASE}/api/v1/admin/agent-graph-tools/http/${encodeURIComponent(id)}`, {
      method: "DELETE",
      headers: authHeader(t),
    });
    if (!resp.ok) {
      alert(await resp.text());
      return;
    }
    await safeLoad();
  });
  document.getElementById("create-user-btn")?.addEventListener("click", async () => {
    const t = (document.getElementById("superadmin-token") as HTMLInputElement)?.value.trim() || getSuperadminTokenForUi();
    const email = (document.getElementById("new-user-email") as HTMLInputElement).value.trim();
    const password = (document.getElementById("new-user-password") as HTMLInputElement).value;
    const display_name = (document.getElementById("new-user-name") as HTMLInputElement).value.trim();
    if (!email || !password) {
      alert("Email и password обязательны");
      return;
    }
    const resp = await fetch(`${GATEWAY_BASE}/api/v1/admin/users`, {
      method: "POST",
      headers: { ...authHeader(t), "Content-Type": "application/json" },
      body: JSON.stringify({ email, password, display_name: display_name || undefined }),
    });
    if (!resp.ok) {
      alert(await resp.text());
      return;
    }
    (document.getElementById("new-user-password") as HTMLInputElement).value = "";
    await safeLoad();
  });
  void safeLoad();
  setupAnsibleTemplates();
  superadminPhasePollTimer = window.setInterval(() => {
    void loadSuperadminRunsLive();
  }, 3000);
}

function renderRoute(): void {
  const r = route();
  if (r !== "/admin" && superadminPhasePollTimer !== null) {
    window.clearInterval(superadminPhasePollTimer);
    superadminPhasePollTimer = null;
  }
  if (isPortalUserSession() && (r === "/dashboard" || r === "/superadmin" || r === "/admin")) {
    location.replace(`${location.pathname}${location.search}#/portal/home`);
    return;
  }
  if (r.startsWith("/portal")) {
    const norm = r === "/portal" || r === "/portal/" ? "/portal/home" : r;
    if (r === "/portal" || r === "/portal/") {
      location.replace(`${location.pathname}${location.search}#/portal/home`);
      return;
    }
    const w = window as unknown as { __lpPortalMounted?: boolean };
    if (w.__lpPortalMounted && document.getElementById("lp-portal-outlet")) {
      syncPortalView(portalViewFromRoute(norm));
      return;
    }
    renderPortal();
    syncPortalView(portalViewFromRoute(norm));
    return;
  }
  if (r === "/superadmin") {
    renderSuperadminLogin();
    return;
  }
  if (r === "/admin") {
    if (!localStorage.getItem(LS_SUPERADMIN_TOKEN)?.trim()) {
      location.replace(`${location.pathname}${location.search}#/superadmin`);
      return;
    }
    renderSuperadmin();
    return;
  }
  if (r === "/login") {
    renderLogin();
    return;
  }
  renderDashboard();
}

if ((window as unknown as { __LAMBOT_APP?: string }).__LAMBOT_APP === "admin") {
  function renderAdminRoute(): void {
    const r = route();
    if (r === "/superadmin") {
      renderSuperadminLogin();
      return;
    }
    if (r === "/admin") {
      if (!localStorage.getItem(LS_SUPERADMIN_TOKEN)?.trim()) {
        location.hash = "#/superadmin";
        return;
      }
      renderSuperadmin();
      return;
    }
    if (r === "/login" || r === "/dashboard") {
      renderDashboard();
      return;
    }
    location.hash = "#/superadmin";
  }
  window.addEventListener("hashchange", renderAdminRoute);
  renderAdminRoute();
} else {
  window.addEventListener("hashchange", renderRoute);
  renderRoute();
}
