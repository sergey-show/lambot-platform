/**
 * Project portal domain: view routing within #/portal/...
 */

const ALLOWED = new Set([
  "home",
  "team",
  "docs",
  "knowledge",
  "tracker",
  "servers",
  "k8s-install",
  "k8s",
  "infra",
  "service",
]);

export function portalViewFromRoute(routePath: string): string {
  if (routePath === "/portal" || routePath === "/portal/") return "home";
  const m = /^\/portal\/([^/?]+)/.exec(routePath);
  const v = m ? m[1] : "home";
  return ALLOWED.has(v) ? v : "home";
}

export function syncPortalView(activeView: string): void {
  const v = ALLOWED.has(activeView) ? activeView : "home";
  document.querySelectorAll(".lp-portal-view").forEach((el) => {
    const id = el.id.replace(/^view-/, "");
    el.classList.toggle("lp-portal-view--active", id === v);
  });
  document.querySelectorAll(".lp-portal-nav a[data-portal-view]").forEach((a) => {
    const is = (a as HTMLElement).dataset.portalView === v;
    a.classList.toggle("lp-portal-nav-active", is);
  });
}

export function bindPortalNav(): void {
  document.querySelectorAll(".lp-portal-nav a[data-portal-view]").forEach((a) => {
    a.addEventListener("click", (e) => {
      const href = (a as HTMLAnchorElement).getAttribute("href");
      if (href && href.startsWith("#")) {
        e.preventDefault();
        location.hash = href.slice(1);
      }
    });
  });
}
