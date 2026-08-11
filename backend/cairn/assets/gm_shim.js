/*
 * A small Tampermonkey-compatible surface, injected ahead of a userscript.
 *
 * A bare browser has none of the GM_* API, so a script that calls
 * GM_setValue throws on its first line and dismisses nothing — and the mint
 * reports "no cookies produced", which is true and useless. Most
 * interstitial-dismissal scripts use none of this; providing it turns the
 * ones that do from a cryptic failure into a working profile (docs/06).
 *
 * What is deliberately NOT here: GM_download, GM_notification, GM_openInTab,
 * GM_registerMenuCommand and the rest of the interactive surface. There is no
 * user watching a mint, so a menu command has nothing to attach to, and a
 * download would write into the container. `userscripts.py` reads the @grant
 * lines and warns about anything this file does not cover, rather than
 * letting it fail at runtime.
 */
(function () {
  "use strict";

  var PREFIX = "__cairn_gm__";

  function key(name) {
    return PREFIX + name;
  }

  // localStorage-backed, so values survive the navigations an interstitial
  // bypass makes — which is the whole reason a script stores anything.
  window.GM_setValue = function (name, value) {
    try {
      window.localStorage.setItem(key(name), JSON.stringify(value));
    } catch (e) {
      /* storage disabled or full; a userscript cannot do anything useful with
         the failure, and throwing here would abort a script that was only
         caching something optional. */
    }
  };

  window.GM_getValue = function (name, fallback) {
    try {
      var raw = window.localStorage.getItem(key(name));
      return raw === null ? fallback : JSON.parse(raw);
    } catch (e) {
      return fallback;
    }
  };

  window.GM_deleteValue = function (name) {
    try {
      window.localStorage.removeItem(key(name));
    } catch (e) {
      /* as above */
    }
  };

  window.GM_listValues = function () {
    var out = [];
    try {
      for (var i = 0; i < window.localStorage.length; i++) {
        var k = window.localStorage.key(i);
        if (k && k.indexOf(PREFIX) === 0) out.push(k.slice(PREFIX.length));
      }
    } catch (e) {
      /* as above */
    }
    return out;
  };

  window.GM_addStyle = function (css) {
    var el = document.createElement("style");
    el.textContent = css;
    (document.head || document.documentElement).appendChild(el);
    return el;
  };

  window.GM_log = function () {
    try {
      console.log.apply(console, arguments);
    } catch (e) {
      /* console can be missing in odd contexts */
    }
  };

  /*
   * fetch-backed, with the callback shape scripts expect. Note this does NOT
   * reproduce Tampermonkey's cross-origin privilege: there the request comes
   * from the extension and ignores CORS, here it is an ordinary page request
   * and the browser enforces same-origin rules. A script relying on that
   * privilege will fail — which userscripts.py warns about, because the
   * alternative is a silent difference in behaviour.
   */
  window.GM_xmlhttpRequest = function (options) {
    options = options || {};
    var method = (options.method || "GET").toUpperCase();
    var controller = typeof AbortController !== "undefined" ? new AbortController() : null;

    var init = {
      method: method,
      headers: options.headers || {},
      credentials: options.anonymous ? "omit" : "include",
      redirect: "follow",
    };
    if (options.data !== undefined && method !== "GET" && method !== "HEAD") {
      init.body = options.data;
    }
    if (controller) init.signal = controller.signal;

    fetch(options.url, init)
      .then(function (response) {
        return response.text().then(function (text) {
          var result = {
            readyState: 4,
            status: response.status,
            statusText: response.statusText,
            responseText: text,
            response: text,
            finalUrl: response.url,
            responseHeaders: "",
          };
          try {
            var parts = [];
            response.headers.forEach(function (value, name) {
              parts.push(name + ": " + value);
            });
            result.responseHeaders = parts.join("\r\n");
          } catch (e) {
            /* headers not enumerable */
          }
          if (options.onload) options.onload(result);
        });
      })
      .catch(function (error) {
        if (options.onerror) options.onerror({ error: String(error), status: 0 });
      });

    return {
      abort: function () {
        if (controller) controller.abort();
      },
    };
  };

  /*
   * In Tampermonkey this reaches the page's own globals from an isolated
   * world. Here the script already runs in the page world, so window is
   * genuinely the same object — the alias exists so scripts that reference it
   * do not throw, and it means what they expect it to mean.
   */
  window.unsafeWindow = window;

  window.GM_info = {
    script: { name: "cairn-mint", version: "1.0", namespace: "cairn" },
    scriptHandler: "cairn",
    version: "1.0",
  };

  // The GM.* promise-based aliases, for scripts written against the newer API.
  window.GM = window.GM || {};
  window.GM.setValue = function (n, v) {
    return Promise.resolve(window.GM_setValue(n, v));
  };
  window.GM.getValue = function (n, d) {
    return Promise.resolve(window.GM_getValue(n, d));
  };
  window.GM.deleteValue = function (n) {
    return Promise.resolve(window.GM_deleteValue(n));
  };
  window.GM.listValues = function () {
    return Promise.resolve(window.GM_listValues());
  };
  window.GM.addStyle = function (c) {
    return Promise.resolve(window.GM_addStyle(c));
  };
  window.GM.xmlHttpRequest = window.GM_xmlhttpRequest;
})();
