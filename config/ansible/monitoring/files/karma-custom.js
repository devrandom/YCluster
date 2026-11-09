// Adds a "YCluster" link back to the admin home for cluster admins.
//
// Karma has no OIDC flow of its own: nginx's Authentik forward-auth adds
// X-Authentik-UID (identity) and X-Authentik-Groups (authorisation) to every
// request, and Karma exposes the parsed groups in /alerts.json. Fetch that
// once and only render the link when ycluster-admins is present.
(function () {
  "use strict";

  var ADMIN_GROUP = "ycluster-admins";
  var LINK_ID = "yc-home-link";
  var STYLE_ID = "yc-home-link-style";

  function addStyle() {
    if (document.getElementById(STYLE_ID)) return;
    var style = document.createElement("style");
    style.id = STYLE_ID;
    style.textContent =
      "#" + LINK_ID + "{" +
        "position:fixed;right:1rem;bottom:1rem;z-index:2000;" +
        "padding:.35rem .7rem;border-radius:6px;" +
        "background:#2b2f36;color:#fff;text-decoration:none;" +
        "font:600 13px/1.2 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;" +
        "opacity:.85;" +
      "}" +
      "#" + LINK_ID + ":hover{opacity:1;color:#fff;text-decoration:none;}";
    document.head.appendChild(style);
  }

  function addLink() {
    if (document.getElementById(LINK_ID)) return;
    var link = document.createElement("a");
    link.id = LINK_ID;
    link.href = "/";
    link.textContent = "YCluster";
    link.title = "Back to the YCluster home page";
    document.body.appendChild(link);
  }

  function isAdmin(groups) {
    return Array.isArray(groups) && groups.indexOf(ADMIN_GROUP) !== -1;
  }

  function check() {
    fetch("alerts.json", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filters: [] }),
      credentials: "same-origin",
    })
      .then(function (response) {
        return response.ok ? response.json() : null;
      })
      .then(function (data) {
        var auth = data && data.authentication;
        if (auth && isAdmin(auth.groups)) {
          addStyle();
          addLink();
        }
      })
      .catch(function () {
        // The link is cosmetic; a failed probe must not affect the dashboard.
      });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", check);
  } else {
    check();
  }
})();
