async function saveBranding(form, url, file) {
    const kind = form.dataset.kind;
    const status = document.getElementById(kind + "-status");
    const buttons = form.querySelectorAll("button");
    buttons.forEach((button) => { button.disabled = true; });
    status.textContent = "Änderung wird gespeichert …";
    try {
        const headers = { "X-Omada-Upload": kind };
        if (file) headers["Content-Type"] = "image/png";
        const response = await fetch(url, { method: "POST", headers, body: file });
        if (response.ok) {
            window.location.reload();
            return;
        }
        status.textContent = await response.text();
    } catch {
        status.textContent = "Speichern fehlgeschlagen. Prüfe die Verbindung und versuche es erneut.";
    } finally {
        buttons.forEach((button) => { button.disabled = false; });
    }
}

document.querySelectorAll(".brand-upload").forEach((form) => {
    form.addEventListener("submit", (event) => {
        event.preventDefault();
        const file = form.querySelector('input[type="file"]').files[0];
        if (!file || file.size > 2 * 1024 * 1024) {
            document.getElementById(form.dataset.kind + "-status").textContent = "Wähle ein PNG mit maximal 2 MiB.";
            return;
        }
        saveBranding(form, "/admin/" + form.dataset.kind, file);
    });
});
const reset = document.getElementById("favicon-reset");
reset.addEventListener("click", () => saveBranding(reset.form, "/admin/favicon/reset"));
