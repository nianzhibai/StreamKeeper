/*
 * Applies the stored appearance preferences before the first paint.
 * Loaded as a render-blocking classic script because the page CSP forbids
 * inline scripts. Both the colour theme and the sidebar width live here so the
 * layout never flashes at the wrong size on load.
 */
(function () {
  var THEME_KEY = "stream-keeper-theme";
  var SIDEBAR_KEY = "stream-keeper-sidebar";
  var root = document.documentElement;
  var media = window.matchMedia("(prefers-color-scheme: light)");
  var preference = "system";
  var sidebar = "full";

  function read(key) {
    try {
      return window.localStorage.getItem(key);
    } catch (error) {
      /* Private browsing modes can block storage; defaults still work. */
      return null;
    }
  }

  function write(key, value) {
    try {
      if (value === null) window.localStorage.removeItem(key);
      else window.localStorage.setItem(key, value);
    } catch (error) {
      /* Ignore storage failures and keep the in-memory preference. */
    }
  }

  var storedTheme = read(THEME_KEY);
  if (storedTheme === "light" || storedTheme === "dark") preference = storedTheme;
  if (read(SIDEBAR_KEY) === "rail") sidebar = "rail";

  function resolved() {
    if (preference === "system") return media.matches ? "light" : "dark";
    return preference;
  }

  function apply() {
    root.dataset.theme = resolved();
    root.dataset.themePreference = preference;
    root.dataset.sidebar = sidebar;
  }

  apply();

  window.streamKeeperTheme = {
    preference: function () {
      return preference;
    },
    resolved: resolved,
    set: function (next) {
      preference = next === "light" || next === "dark" ? next : "system";
      write(THEME_KEY, preference === "system" ? null : preference);
      apply();
      return preference;
    },
    sidebar: function () {
      return sidebar;
    },
    setSidebar: function (next) {
      sidebar = next === "rail" ? "rail" : "full";
      write(SIDEBAR_KEY, sidebar === "rail" ? "rail" : null);
      apply();
      return sidebar;
    },
  };

  media.addEventListener("change", function () {
    if (preference === "system") apply();
  });
})();
