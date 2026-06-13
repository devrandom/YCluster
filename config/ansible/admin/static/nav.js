// Shared nav bar for the admin web pages — the single source of truth for
// page links. Each page includes exactly one line:
//   <script src="/static/nav.js" defer></script>
// The bar self-injects at the top of <body>; the current page is
// highlighted. Also injects the YCluster favicon (same brand assets as the
// IdP pages). The Account entry points at the IdP on the auth. sibling of
// whatever host serves the page (admin.xc -> auth.xc, admin.<domain> ->
// auth.<domain>); any element with id="account-link" gets the same href.
(function () {
    const favicon = document.createElement('link');
    favicon.rel = 'icon';
    favicon.type = 'image/svg+xml';
    favicon.href = '/static/ycluster-favicon.svg';
    document.head.appendChild(favicon);

    // The user-facing host (app.<domain>) serves only the self-service
    // scheduling page; the admin host (admin.<domain> / admin.xc) serves the
    // full set. Show each its own links so app.'s nav doesn't point at admin
    // pages that just bounce back. Account always targets the auth. sibling.
    const isApp = location.hostname.startsWith('app.');
    const authHref = location.protocol + '//' +
        location.hostname.replace(/^(admin|app)\./, 'auth.') + '/';

    const items = isApp ? [
        ['VM Schedule', '/admin/vm-schedule'],
        ['Account', authHref],
    ] : [
        ['Home', '/'],
        ['Status', '/status'],
        ['Utilization', '/admin/utilization'],
        ['Inventory', '/admin/inventory'],
        ['VM Schedule', '/admin/vm-schedule'],
        ['VM Usage', '/admin/vm-usage'],
        ['Model Usage', '/admin/model-usage'],
        ['Users', '/admin/users'],
        ['Monitoring', '/grafana/dashboards'],
        ['Account', authHref],
    ];
    const brand = isApp ? 'YCluster' : 'YCluster Admin';

    const style = document.createElement('style');
    style.textContent = `
        #yc-nav {
            display: flex; gap: 0.25rem; align-items: center; flex-wrap: wrap;
            background: white; border: 1px solid #d8dce1; border-radius: 6px;
            padding: 0.4rem 0.75rem; margin-bottom: 1.25rem;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            font-size: 0.875rem;
        }
        #yc-nav .brand { font-weight: 700; color: #1f2328; margin-right: 0.75rem; }
        #yc-nav .brand img { width: 18px; height: 18px; vertical-align: -3px; margin-right: 0.4rem; }
        #yc-nav a {
            color: #0a5dc2; text-decoration: none;
            padding: 0.2rem 0.55rem; border-radius: 4px;
        }
        #yc-nav a:hover { background: #f0f4fa; }
        #yc-nav a.cur { background: #e8ebee; color: #1f2328; font-weight: 600; }
    `;
    document.head.appendChild(style);

    const here = location.pathname.replace(/\/+$/, '') || '/';
    const nav = document.createElement('nav');
    nav.id = 'yc-nav';
    nav.innerHTML = '<span class="brand"><img src="/static/ycluster-favicon.svg" alt="">' + brand + '</span>' + items.map(function (it) {
        const cur = it[1] === here ? ' class="cur"' : '';
        return '<a href="' + it[1] + '"' + cur + '>' + it[0] + '</a>';
    }).join('');
    document.body.prepend(nav);

    const acct = document.getElementById('account-link');
    if (acct) acct.href = authHref;
})();
