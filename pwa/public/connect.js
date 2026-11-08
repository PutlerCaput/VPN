// Kept out of connect.html: the site's Content-Security-Policy allows scripts
// from this origin only, so an inline script would never run.
(function () {
  "use strict";
  // The only subscription address this page hands to a VPN app. Keep it equal to
  // SUBSCRIPTION_BASE_URL in .env.example: a test checks that it is.
  var SUB_ORIGIN = "https://sub.cyberdefency.com:8443";
  // Custom URL schemes of the supported apps.
  var APPS = {
    happ: "happ://add/",
    v2raytun: "v2raytun://import/",
    hiddify: "hiddify://import/"
  };

  function show(id) {
    document.getElementById(id).hidden = false;
  }

  function isSubscriptionLink(value) {
    try {
      var url = new URL(value);
      return url.protocol === "https:" && url.origin === SUB_ORIGIN && url.pathname.indexOf("/sub/") === 0;
    } catch (e) {
      return false;
    }
  }

  // The link travels in the fragment, which browsers never send to the server,
  // so it stays out of the site's access logs.
  var params = new URLSearchParams(location.hash.slice(1));
  var app = params.get("app") || "";
  var sub = params.get("sub") || "";
  if (!Object.prototype.hasOwnProperty.call(APPS, app) || !isSubscriptionLink(sub)) {
    show("error");
    return;
  }

  var deepLink = APPS[app] + sub;
  document.getElementById("open").setAttribute("href", deepLink);
  document.getElementById("sub").value = sub;
  show("ok");

  // Try once on arrival; the button stays for browsers that ask first.
  setTimeout(function () { location.href = deepLink; }, 200);

  document.getElementById("copy").addEventListener("click", function () {
    var field = document.getElementById("sub");
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(sub).then(
        function () { show("copied"); },
        function () { field.select(); }
      );
    } else {
      field.select();
    }
  });
})();
