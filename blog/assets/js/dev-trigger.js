(function () {
  const API_BASE = "http://localhost:8000";

  function setStatus(status, className, message) {
    status.className = "trigger-status";
    if (className) {
      status.classList.add(className);
    }
    status.textContent = message;
  }

  async function readResponse(res) {
    const text = await res.text();
    if (!text) {
      return {};
    }

    try {
      return JSON.parse(text);
    } catch (_err) {
      throw new Error(`API returned ${res.status}: ${text.slice(0, 160)}`);
    }
  }

  function failureMessage(data) {
    return data.error || data.message || data.detail || data.last_result || "Pipeline failed without details.";
  }

  async function pollStatus(btn, status) {
    const interval = setInterval(async () => {
      try {
        const res = await fetch(`${API_BASE}/trigger/status`);
        const data = await readResponse(res);

        if (!res.ok) {
          throw new Error(failureMessage(data) || `HTTP ${res.status}`);
        }

        if (data.running) {
          setStatus(status, "", data.message || "Pipeline running...");
          return;
        }

        clearInterval(interval);
        btn.disabled = false;
        btn.setAttribute("aria-busy", "false");

        if (data.last_result === "success") {
          setStatus(status, "status-ok", data.message || "Done. Reload the page to see the new post.");
        } else {
          setStatus(status, "status-err", failureMessage(data));
        }
      } catch (err) {
        clearInterval(interval);
        btn.disabled = false;
        btn.setAttribute("aria-busy", "false");
        setStatus(status, "status-err", err.message || "Lost connection to API");
      }
    }, 3000);
  }

  async function triggerUpdate(btn, status) {
    btn.disabled = true;
    btn.setAttribute("aria-busy", "true");
    setStatus(status, "", "Starting pipeline...");

    try {
      const res = await fetch(`${API_BASE}/trigger`, { method: "POST" });
      const data = await readResponse(res);

      if (!res.ok) {
        throw new Error(data.detail || failureMessage(data) || `HTTP ${res.status}`);
      }

      if (data.status === "already_running") {
        setStatus(status, "status-warn", data.message || "Already running");
        btn.disabled = false;
        btn.setAttribute("aria-busy", "false");
        return;
      }

      setStatus(status, "status-ok", data.message || "Pipeline started. Polling for completion...");
      pollStatus(btn, status);
    } catch (err) {
      setStatus(status, "status-err", err.message || "Could not start pipeline");
      btn.disabled = false;
      btn.setAttribute("aria-busy", "false");
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    const btn = document.getElementById("trigger-btn");
    const status = document.getElementById("trigger-status");

    if (!btn || !status) {
      return;
    }

    btn.addEventListener("click", () => triggerUpdate(btn, status));
  });
})();
