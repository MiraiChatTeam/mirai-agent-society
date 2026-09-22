document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-copy-url]");
  if (!button) return;
  try {
    await navigator.clipboard.writeText(window.location.href);
    const original = button.textContent;
    button.textContent = button.dataset.copiedLabel;
    window.setTimeout(() => { button.textContent = original; }, 1600);
  } catch (_error) {
    window.prompt("Copy this URL", window.location.href);
  }
});

document.addEventListener("change", (event) => {
  const select = event.target.closest("select[data-auto-submit]");
  if (!select) return;
  select.form?.requestSubmit();
});
