/* GE-EHR client behaviour. CSP-safe: no inline scripts or handlers; everything is wired
   through data attributes.

   data-confirm="message"     on a submit button or form: ask in a dialog before submitting
     data-confirm-title, data-confirm-label, data-confirm-variant ("danger" | "primary")
   data-no-disable            on a POST form: keep buttons enabled after submit (e.g. downloads)
   data-autodismiss="ms"      on an .alert: close it after the delay (paused while hovered/focused)
   data-bs-toggle="tooltip"   Bootstrap tooltip
   data-max-bytes="n"         on a file input: warn and block submission above n bytes
   data-autosubmit            on a select/checkbox: submit its form on change
   data-history-back          on a link: go back in history when possible, else follow href
   data-width="0-100"         set an element's width in percent (progress bars without inline styles)
*/
(function () {
  "use strict";

  var bs = window.bootstrap;
  var SUBMIT_SELECTOR = 'button[type="submit"], button:not([type]), input[type="submit"]';

  function formatBytes(bytes) {
    if (bytes < 1024) return bytes + " bytes";
    var units = ["KB", "MB", "GB"];
    var value = bytes / 1024;
    var unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024;
      unit += 1;
    }
    return value.toFixed(1) + " " + units[unit];
  }

  /* ---- Confirmation dialog ---------------------------------------------------------- */
  var modalElement = document.getElementById("confirm-modal");

  function askConfirmation(source) {
    var message = source.getAttribute("data-confirm");
    if (!modalElement || !bs) return Promise.resolve(window.confirm(message));
    return new Promise(function (resolve) {
      var modal = bs.Modal.getOrCreateInstance(modalElement);
      var accept = modalElement.querySelector("[data-confirm-accept]");
      var accepted = false;
      modalElement.querySelector("[data-confirm-message]").textContent = message;
      modalElement.querySelector("[data-confirm-title]").textContent =
        source.getAttribute("data-confirm-title") || "Please confirm";
      accept.textContent = source.getAttribute("data-confirm-label") || "Confirm";
      accept.className = "btn btn-" + (source.getAttribute("data-confirm-variant") === "danger" ? "danger" : "primary");
      function onAccept() {
        accepted = true;
        modal.hide();
      }
      accept.addEventListener("click", onAccept);
      modalElement.addEventListener("shown.bs.modal", function () { accept.focus(); }, { once: true });
      modalElement.addEventListener("hidden.bs.modal", function () {
        accept.removeEventListener("click", onAccept);
        resolve(accepted);
      }, { once: true });
      modal.show();
    });
  }

  function setBusy(form, busy) {
    form.querySelectorAll(SUBMIT_SELECTOR).forEach(function (button) { button.disabled = busy; });
    if (busy) form.setAttribute("aria-busy", "true");
    else form.removeAttribute("aria-busy");
  }

  function markSubmitting(form) {
    if ((form.getAttribute("method") || "get").toLowerCase() !== "post" || form.hasAttribute("data-no-disable")) return;
    /* Deferred so the clicked button's name/value is still part of the submission. */
    window.setTimeout(function () { setBusy(form, true); }, 0);
  }

  function oversizedFile(form) {
    return form.querySelector('input[type="file"][data-too-large="true"]');
  }

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    var tooLarge = oversizedFile(form);
    if (tooLarge) {
      event.preventDefault();
      tooLarge.focus();
      return;
    }
    if (form.dataset.confirmed === "true") {
      delete form.dataset.confirmed;
      markSubmitting(form);
      return;
    }
    var submitter = event.submitter || null;
    var source = submitter && submitter.hasAttribute("data-confirm") ? submitter
      : (form.hasAttribute("data-confirm") ? form : null);
    if (!source) {
      markSubmitting(form);
      return;
    }
    event.preventDefault();
    askConfirmation(source).then(function (ok) {
      if (!ok) return;
      form.dataset.confirmed = "true";
      if (typeof form.requestSubmit === "function") form.requestSubmit(submitter || undefined);
      else form.submit();
    });
  });

  /* Re-enable buttons when the page is restored from the back/forward cache. */
  window.addEventListener("pageshow", function (event) {
    if (!event.persisted) return;
    document.querySelectorAll('form[aria-busy="true"]').forEach(function (form) { setBusy(form, false); });
  });

  /* ---- Tooltips --------------------------------------------------------------------- */
  if (bs) {
    document.querySelectorAll('[data-bs-toggle="tooltip"]').forEach(function (element) {
      bs.Tooltip.getOrCreateInstance(element);
    });
  }

  /* ---- Auto-dismissing flash messages ----------------------------------------------- */
  document.querySelectorAll(".alert[data-autodismiss]").forEach(function (alert) {
    var delay = parseInt(alert.getAttribute("data-autodismiss"), 10) || 6000;
    var timer = null;
    function close() {
      if (bs) bs.Alert.getOrCreateInstance(alert).close();
      else alert.remove();
    }
    function start() { timer = window.setTimeout(close, delay); }
    function stop() { window.clearTimeout(timer); }
    alert.addEventListener("mouseenter", stop);
    alert.addEventListener("mouseleave", start);
    alert.addEventListener("focusin", stop);
    alert.addEventListener("focusout", start);
    start();
  });

  /* ---- File inputs: show name and size, enforce data-max-bytes ---------------------- */
  document.querySelectorAll('input[type="file"]').forEach(function (input) {
    var info = document.createElement("div");
    info.className = "form-text file-info";
    info.setAttribute("aria-live", "polite");
    input.insertAdjacentElement("afterend", info);
    input.addEventListener("change", function () {
      var file = input.files && input.files[0];
      var max = parseInt(input.getAttribute("data-max-bytes") || "0", 10);
      var tooLarge = Boolean(file && max > 0 && file.size > max);
      input.dataset.tooLarge = tooLarge ? "true" : "false";
      input.classList.toggle("is-invalid", tooLarge);
      info.classList.toggle("is-too-large", tooLarge);
      if (!file) {
        info.textContent = "";
      } else if (tooLarge) {
        info.textContent = file.name + " is " + formatBytes(file.size) + "; the maximum is " + formatBytes(max) + ".";
      } else {
        info.textContent = "Selected: " + file.name + " (" + formatBytes(file.size) + ")";
      }
    });
  });

  /* ---- Auto-submitting filters, history-back links, percentage widths --------------- */
  document.addEventListener("change", function (event) {
    var control = event.target.closest ? event.target.closest("[data-autosubmit]") : null;
    if (!control || !control.form) return;
    if (typeof control.form.requestSubmit === "function") control.form.requestSubmit();
    else control.form.submit();
  });

  document.addEventListener("click", function (event) {
    var link = event.target.closest ? event.target.closest("[data-history-back]") : null;
    if (!link || window.history.length <= 1) return;
    event.preventDefault();
    window.history.back();
  });

  document.querySelectorAll("[data-width]").forEach(function (element) {
    var value = parseFloat(element.getAttribute("data-width"));
    if (!isNaN(value)) element.style.width = Math.max(0, Math.min(100, value)) + "%";
  });
})();
