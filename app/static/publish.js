/* 夜班发布页：补批采信 + 滚动发布，成功后刷新以展示最新版本与五账。 */
(function () {
  "use strict";

  async function postJSON(url, payload) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {})
    });
    const body = await response.json().catch(function () { return {}; });
    if (!response.ok) {
      throw new Error(body.message || body.error || "HTTP " + response.status);
    }
    return body;
  }

  function bind(formId, btnId, hintId, url, buildPayload, successText) {
    const form = document.getElementById(formId);
    if (!form) {
      return;
    }
    const button = document.getElementById(btnId);
    const hint = document.getElementById(hintId);
    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      if (button.disabled) {
        return;
      }
      button.disabled = true;
      hint.className = "hint";
      hint.textContent = "处理中";
      try {
        const result = await postJSON(url, buildPayload());
        const reused = result.reused ? "（幂等复用，未新增）" : "";
        hint.className = "hint ok";
        hint.textContent = successText(result) + reused;
        window.setTimeout(function () { window.location.reload(); }, 600);
      } catch (error) {
        hint.className = "hint bad";
        hint.textContent = "失败：" + error.message;
      } finally {
        button.disabled = false;
      }
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    bind(
      "ingest-night-form",
      "ingest-night-btn",
      "ingest-night-hint",
      "/api/publish/batches/ingest-sample",
      function () {
        return { sample: document.getElementById("night-sample").value };
      },
      function (r) {
        return "已采信 " + r.batch_id + "，原始 " + r.raw_rows + " / 采信 " + r.accepted;
      }
    );

    bind(
      "publish-form",
      "publish-btn",
      "publish-hint",
      "/api/publish/publish",
      function () {
        return {
          cutoff: document.getElementById("publish-cutoff").value.trim() || null,
          note: document.getElementById("publish-note").value.trim()
        };
      },
      function (r) {
        return "发布 " + r.publish_id + "：新 " + r.new_versions +
          " / 修订 " + r.revised_versions + " / 不变 " + r.unchanged_versions;
      }
    );
  });
})();
