/*
 * Applies the stored colour theme before the first paint.
 * Loaded as a render-blocking classic script because the page CSP forbids inline scripts.
 */
(function () {
  var STORAGE_KEY = "stream-keeper-theme";
  var root = document.documentElement;
  var media = window.matchMedia("(prefers-color-scheme: light)");
  var preference = "system";

  try {
    var stored = window.localStorage.getItem(STORAGE_KEY);
    if (stored === "light" || stored === "dark") preference = stored;
  } catch (error) {
    /* Private browsing modes can block storage; the system theme still works. */
  }

  function resolved() {
    if (preference === "system") return media.matches ? "light" : "dark";
    return preference;
  }

  function apply() {
    root.dataset.theme = resolved();
    root.dataset.themePreference = preference;
  }

  apply();

  window.streamKeeperTheme = {
    preference: function () {
      return preference;
    },
    resolved: resolved,
    set: function (next) {
      preference = next === "light" || next === "dark" ? next : "system";
      try {
        if (preference === "system") window.localStorage.removeItem(STORAGE_KEY);
        else window.localStorage.setItem(STORAGE_KEY, preference);
      } catch (error) {
        /* Ignore storage failures and keep the in-memory preference. */
      }
      apply();
      return preference;
    },
  };

  media.addEventListener("change", function () {
    if (preference === "system") apply();
  });
})();
