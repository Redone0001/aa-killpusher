(() => {
  "use strict";
  const root = document.querySelector("#killpusher");
  if (!root) return;
  const csrf = document.querySelector("#kp-csrf input").value;
  const feedback = document.querySelector("#kp-feedback");
  const tell = (message, danger = false) => {
    feedback.textContent = message;
    feedback.className = `alert ${danger ? "alert-warning" : "alert-info"}`;
  };
  const update = (id, result) => {
    root.querySelectorAll(`tr[data-killmail="${id}"]`).forEach((row) => {
      row.querySelector(".kp-state").textContent = result.label;
      row.querySelector(".kp-message").textContent = result.message;
      row.querySelector(".kp-push").disabled = !result.can_push;
      row.querySelector(".kp-check").classList.toggle("d-none", !result.can_check);
    });
  };
  const send = async (url, confirmation = "") => {
    const response = await fetch(url, {
      method: "POST", credentials: "same-origin",
      headers: { "X-CSRFToken": csrf, "Accept": "application/json" },
      body: new URLSearchParams({ confirmation }),
    });
    if (response.redirected || !response.headers.get("content-type")?.includes("application/json")) {
      throw new Error("Session or permission changed. Refresh the page before continuing.");
    }
    const data = await response.json();
    if (data.state === "confirmation_required") {
      if (window.confirm(data.message)) return send(url, data.confirmation);
      return null;
    }
    if (!response.ok) throw new Error(data.message || "Request failed. Refresh to check its status.");
    return data;
  };
  root.addEventListener("click", async (event) => {
    const button = event.target.closest("button");
    if (button?.id === "kp-refresh") {
      button.disabled = true;
      try {
        const result = await send(button.dataset.url);
        tell(result.message, result.failed > 0);
        document.querySelector("#kp-reload").classList.remove("d-none");
      } catch (error) {
        tell(error.message || "Could not request imports. Try again later.", true);
      } finally {
        button.disabled = false;
      }
      return;
    }
    const row = button?.closest("tr[data-killmail]");
    if (!row) return;
    if (button.classList.contains("kp-copy")) {
      const input = row.querySelector(".kp-url");
      try {
        await navigator.clipboard.writeText(input.value);
        tell("ESI URL copied. Paste it on zKillboard’s posting page.");
      } catch (_) {
        input.focus(); input.select();
        tell("URL selected. Press Ctrl+C or Command+C to copy it.");
      }
      return;
    }
    if (!button.classList.contains("kp-push") && !button.classList.contains("kp-check")) return;
    const id = row.dataset.killmail;
    const related = [...root.querySelectorAll(`tr[data-killmail="${id}"] .kp-push, tr[data-killmail="${id}"] .kp-check`)];
    const prior = related.map((item) => item.disabled);
    related.forEach((item) => { item.disabled = true; });
    try {
      const result = await send(button.dataset.url);
      if (result) {
        related.forEach((item) => { item.disabled = false; });
        update(id, result);
        tell(result.message, result.state !== "submitted");
      } else {
        related.forEach((item, i) => { item.disabled = prior[i]; });
      }
    } catch (error) {
      // The server's durable claim prevents a retry from sending a duplicate.
      related.forEach((item, i) => { item.disabled = prior[i]; });
      tell(error.message || "Connection interrupted. Refresh to check the posting status.", true);
    }
  });
})();
