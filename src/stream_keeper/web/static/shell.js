/*
 * Injects the chrome every page shares: skip link, sidebar, toast region and the
 * shared confirm dialog.
 *
 * Loaded as a render-blocking classic script rather than an ES module, on
 * purpose. As a module it was deferred behind the whole import graph, so the
 * sidebar landed a frame after the page content and every navigation looked like
 * a rebuild of the one element that never changes. Running before the first
 * paint also gets the sidebar into the cross-document view-transition snapshot,
 * which is what lets it stay put between pages.
 *
 * Everything is inserted immediately instead of on DOMContentLoaded so that the
 * toast region and confirm dialog exist before any deferred page module runs.
 * The page CSP forbids inline scripts, hence a separate file.
 */
(function () {
  var NAVIGATION = [
    {
      title: "录制",
      items: [
        {
          page: "dashboard",
          href: "/",
          label: "概览",
          mobileLabel: "概览",
          icon: "dashboard",
          mobile: "primary",
        },
        {
          page: "tasks",
          href: "/tasks",
          label: "录制任务",
          mobileLabel: "任务",
          icon: "record",
          mobile: "primary",
        },
        {
          page: "recordings",
          href: "/recordings",
          label: "本地录像",
          mobileLabel: "录像",
          icon: "video",
          mobile: "primary",
        },
      ],
    },
    {
      title: "运维",
      items: [
        {
          page: "archive",
          href: "/archive",
          label: "网盘归档",
          icon: "cloud",
          mobile: "more",
          description: "管理存储目标与自动上传",
        },
        {
          page: "logs",
          href: "/logs",
          label: "运行日志",
          icon: "logs",
          mobile: "more",
          description: "查看录制、上传和登录事件",
        },
        {
          page: "settings",
          href: "/settings",
          label: "设置",
          icon: "settings",
          mobile: "more",
          description: "调整上传模式与执行计划",
        },
      ],
    },
  ];

  var THEME_MODES = [
    { value: "system", label: "跟随系统", icon: "monitor" },
    { value: "light", label: "浅色", icon: "sun" },
    { value: "dark", label: "深色", icon: "moon" },
  ];

  function icon(name, className) {
    return window.streamKeeperIcon(name, className);
  }

  function brandMarkup(href) {
    // No aria-label: the visible text is the accessible name, and overriding it
    // with a different string trips the "label in name" rule for screen readers.
    return (
      '<a class="brand" href="' + href + '">' +
      '<span class="brand-mark" aria-hidden="true"><i></i></span>' +
      '<span class="brand-text"><strong>Stream Keeper</strong></span>' +
      "</a>"
    );
  }

  function navigationMarkup(current) {
    return NAVIGATION.map(function (group) {
      var links = group.items.map(function (item) {
        var active = item.page === current;
        return (
          '<a class="nav-link' + (active ? " is-active" : "") + '" href="' + item.href +
          '" data-nav="' + item.page + '" data-nav-label="' + item.label + '"' +
          (active ? ' aria-current="page"' : "") + ">" +
          icon(item.icon) + '<span class="nav-label"><span class="nav-label-full">' + item.label +
          '</span><span class="nav-label-compact">' + (item.mobileLabel || item.label) +
          "</span></span></a>"
        );
      }).join("");
      return '<p class="nav-title">' + group.title + "</p>" + links;
    }).join("");
  }

  function mobileMoreItems() {
    return NAVIGATION.reduce(function (items, group) {
      return items.concat(group.items.filter(function (item) { return item.mobile === "more"; }));
    }, []);
  }

  function mobileMoreToggleMarkup(current) {
    var currentItem = mobileMoreItems().find(function (item) { return item.page === current; });
    var label = currentItem ? "更多，当前页面：" + currentItem.label : "更多功能";
    return (
      '<button class="nav-link mobile-more-toggle' + (currentItem ? " is-active" : "") +
      '" type="button" data-mobile-more-toggle aria-label="' + label +
      '" aria-haspopup="dialog" aria-controls="mobile-more-dialog" aria-expanded="false">' +
      icon("more") + '<span class="nav-label">更多</span></button>'
    );
  }

  function mobileMoreDialogMarkup(current) {
    var links = mobileMoreItems().map(function (item) {
      var active = item.page === current;
      return (
        '<a class="mobile-more-link' + (active ? " is-active" : "") + '" href="' + item.href + '"' +
        (active ? ' aria-current="page"' : "") + ">" +
        '<span class="mobile-more-glyph" aria-hidden="true">' + icon(item.icon) + "</span>" +
        '<span class="mobile-more-copy"><strong>' + item.label + "</strong><small>" + item.description +
        "</small></span>" + icon("chevronRight", "ic-sm") + "</a>"
      );
    }).join("");
    return (
      '<dialog id="mobile-more-dialog" class="mobile-more" aria-modal="true" aria-labelledby="mobile-more-title">' +
      '<span class="mobile-more-handle" aria-hidden="true"></span>' +
      '<header class="mobile-more-head"><div><small>导航</small><h2 id="mobile-more-title">更多功能</h2></div>' +
      '<button class="btn btn-icon btn-soft" type="button" data-mobile-more-close aria-label="关闭更多功能">' +
      icon("close", "ic-sm") + "</button></header>" +
      '<nav class="mobile-more-nav" aria-label="更多导航">' + links + "</nav></dialog>"
    );
  }

  function themeSwitchMarkup() {
    var current = (window.streamKeeperTheme && window.streamKeeperTheme.preference()) || "system";
    return (
      '<div class="theme-switch" role="radiogroup" aria-label="外观主题">' +
      THEME_MODES.map(function (mode) {
        return (
          '<button type="button" role="radio" data-theme-mode="' + mode.value +
          '" title="' + mode.label + '" aria-label="' + mode.label +
          '" aria-checked="' + String(mode.value === current) + '">' + icon(mode.icon) + "</button>"
        );
      }).join("") +
      "</div>"
    );
  }

  function sidebarMarkup(current) {
    return (
      '<aside class="sidebar">' +
      '<div class="sidebar-head">' + brandMarkup("/") +
      '<button class="rail-toggle" type="button" data-sidebar-toggle aria-label="收起侧边栏" title="收起侧边栏">' +
      icon("panelLeft", "ic-sm") + "</button></div>" +
      '<nav class="nav" aria-label="主要导航">' + navigationMarkup(current) + mobileMoreToggleMarkup(current) +
      "</nav>" +
      '<div class="sidebar-foot">' +
      '<div id="server-health" class="health" title="服务连接状态"><i></i><span>正在连接</span></div>' +
      '<div class="sidebar-tools">' + themeSwitchMarkup() +
      '<a class="api-link" href="/api/docs" target="_blank" rel="noreferrer">API 文档' +
      icon("external", "ic-sm") + "</a></div>" +
      '<div class="account">' +
      '<span class="avatar avatar-sm" data-account-avatar aria-hidden="true">A</span>' +
      '<span class="account-text"><strong data-account-name>—</strong><small>管理员</small></span>' +
      '<button class="btn btn-icon btn-sm btn-ghost" type="button" data-logout aria-label="退出登录" title="退出登录">' +
      icon("logout", "ic-sm") + "</button></div></div></aside>"
    );
  }

  function confirmDialogMarkup() {
    return (
      '<dialog id="confirm-dialog" class="modal modal-sm" aria-labelledby="confirm-title">' +
      '<div class="modal-head">' +
      '<span class="modal-glyph" data-confirm-glyph aria-hidden="true">' + icon("alert") + "</span>" +
      '<div class="modal-heading"><h2 id="confirm-title" data-confirm-title>确认操作</h2>' +
      "<p data-confirm-message></p></div></div>" +
      '<div class="modal-foot">' +
      '<button class="btn btn-soft" type="button" data-confirm-cancel>取消</button>' +
      '<button class="btn btn-primary" type="button" data-confirm-accept>确定</button>' +
      "</div></dialog>"
    );
  }

  function mountThemeSwitch(root) {
    root.querySelectorAll("[data-theme-mode]").forEach(function (button) {
      button.addEventListener("click", function () {
        var next = (window.streamKeeperTheme && window.streamKeeperTheme.set(button.dataset.themeMode)) || "system";
        root.querySelectorAll("[data-theme-mode]").forEach(function (item) {
          item.setAttribute("aria-checked", String(item.dataset.themeMode === next));
        });
      });
    });
  }

  /**
   * In rail mode the labels are hidden, so the only thing naming a nav item is
   * its tooltip. Adding the attribute permanently would double up with the
   * visible label, so it is applied and removed alongside the width.
   */
  function syncRailLabels(rail, sidebar) {
    sidebar.querySelectorAll("[data-nav-label]").forEach(function (link) {
      if (rail) link.title = link.dataset.navLabel;
      else link.removeAttribute("title");
    });
    var toggle = sidebar.querySelector("[data-sidebar-toggle]");
    if (!toggle) return;
    var label = rail ? "展开侧边栏" : "收起侧边栏";
    toggle.title = label;
    toggle.setAttribute("aria-label", label);
    toggle.setAttribute("aria-expanded", String(!rail));
  }

  function mountMobileMore(root) {
    var toggle = root.querySelector("[data-mobile-more-toggle]");
    var dialog = root.querySelector("#mobile-more-dialog");
    if (!toggle || !dialog || toggle.dataset.mounted === "true") return;
    toggle.dataset.mounted = "true";
    var mobile = window.matchMedia("(max-width: 640px)");

    function syncOpenState(open) {
      toggle.setAttribute("aria-expanded", String(open));
    }

    function close() {
      if (!dialog.open) return;
      if (typeof dialog.close === "function") dialog.close();
      else {
        dialog.removeAttribute("open");
        syncOpenState(false);
      }
    }

    toggle.addEventListener("click", function () {
      if (!mobile.matches || dialog.open) return;
      if (typeof dialog.showModal === "function") dialog.showModal();
      else dialog.setAttribute("open", "");
      syncOpenState(true);
      window.requestAnimationFrame(function () {
        var target = dialog.querySelector('[aria-current="page"]') || dialog.querySelector(".mobile-more-link");
        if (target) target.focus({ preventScroll: true });
      });
    });

    dialog.querySelector("[data-mobile-more-close]").addEventListener("click", close);
    dialog.addEventListener("close", function () { syncOpenState(false); });
    dialog.addEventListener("click", function (event) {
      if (event.target !== dialog) return;
      var bounds = dialog.getBoundingClientRect();
      var outside = event.clientX < bounds.left || event.clientX > bounds.right ||
        event.clientY < bounds.top || event.clientY > bounds.bottom;
      if (outside) close();
    });

    var touchStart = null;
    dialog.addEventListener("touchstart", function (event) {
      if (event.touches.length !== 1) return;
      touchStart = { x: event.touches[0].clientX, y: event.touches[0].clientY };
    }, { passive: true });
    dialog.addEventListener("touchend", function (event) {
      if (!touchStart || event.changedTouches.length !== 1) return;
      var deltaX = Math.abs(event.changedTouches[0].clientX - touchStart.x);
      var deltaY = event.changedTouches[0].clientY - touchStart.y;
      touchStart = null;
      if (deltaY > 72 && deltaY > deltaX * 1.4) close();
    }, { passive: true });
    dialog.addEventListener("touchcancel", function () { touchStart = null; }, { passive: true });

    mobile.addEventListener("change", function (event) {
      if (!event.matches) close();
    });
  }

  function mountSidebarToggle(sidebar) {
    var theme = window.streamKeeperTheme;
    syncRailLabels(theme && theme.sidebar() === "rail", sidebar);
    var toggle = sidebar.querySelector("[data-sidebar-toggle]");
    if (!toggle) return;
    toggle.addEventListener("click", function () {
      var current = theme && theme.sidebar() === "rail";
      var next = (theme && theme.setSidebar(current ? "full" : "rail")) || "full";
      syncRailLabels(next === "rail", sidebar);
    });
  }

  /** Reveals the hairline under the sticky page header once content scrolls beneath it. */
  function watchStickyHeader() {
    var stuck = false;
    function update() {
      var next = window.scrollY > 4;
      if (next === stuck) return;
      stuck = next;
      document.body.classList.toggle("is-stuck", stuck);
    }
    update();
    window.addEventListener("scroll", update, { passive: true });
  }

  function mountShell() {
    if (document.body.classList.contains("login-page")) return;

    if (!document.querySelector(".sidebar")) {
      // The sidebar is fixed-position chrome, so it does not need to sit inside
      // #app — which has not been parsed yet at this point in the document.
      document.body.insertAdjacentHTML(
        "afterbegin",
        '<a class="skip-link" href="#main">跳到主要内容</a>' + sidebarMarkup(document.body.dataset.page) +
        mobileMoreDialogMarkup(document.body.dataset.page),
      );
      var sidebar = document.querySelector(".sidebar");
      mountThemeSwitch(sidebar);
      mountSidebarToggle(sidebar);
      mountMobileMore(document);
    }

    if (!document.querySelector("#toast-region")) {
      document.body.insertAdjacentHTML(
        "beforeend",
        '<div id="toast-region" class="toasts" role="region" aria-live="polite" aria-label="通知"></div>',
      );
    }
    if (!document.querySelector("#confirm-dialog")) {
      document.body.insertAdjacentHTML("beforeend", confirmDialogMarkup());
    }

    watchStickyHeader();
  }

  // The login page has no sidebar but still renders the theme switch itself.
  window.streamKeeperShell = {
    themeSwitchMarkup: themeSwitchMarkup,
    mountThemeSwitch: mountThemeSwitch,
  };

  mountShell();
})();
