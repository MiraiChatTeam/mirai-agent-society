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

document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-copy-prompt]");
  if (!button) return;
  const prompt = document.getElementById(button.dataset.copyPrompt);
  if (!prompt) return;
  const value = prompt.textContent.trim();
  try {
    await navigator.clipboard.writeText(value);
    const original = button.textContent;
    button.textContent = button.dataset.copiedLabel;
    window.setTimeout(() => { button.textContent = original; }, 1600);
  } catch (_error) {
    window.prompt(button.dataset.copyFallback, value);
  }
});
