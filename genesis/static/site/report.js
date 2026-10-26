(() => {
  const { api, $ } = window.weave;
  const site = new URLSearchParams(location.search).get("site");
  if (site) $("#site").value = site;
  $("#report-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    $("#report-error").textContent = "";
    try {
      await api("/api/report", { site: $("#site").value.trim(), reason: $("#reason").value, details: $("#details").value.trim() });
      $("#report-form").hidden = true;
      $("#report-done").hidden = false;
    } catch (err) {
      $("#report-error").textContent = err.message;
    }
  });
})();
