const syncForm = document.querySelector("#network-sync-form");
const syncButton = syncForm.querySelector("button");
syncForm.addEventListener("submit", () => {
    syncButton.disabled = true;
    syncButton.textContent = "Abgleich läuft …";
});

document.querySelectorAll(".network-item").forEach((item) => {
    item.addEventListener("toggle", () => {
        if (!item.open) {
            const secret = item.querySelector(".revealed-secret");
            if (secret) {
                const mask = document.createElement("span");
                mask.setAttribute("aria-label", "Kennwort verborgen");
                mask.textContent = "••••••••";
                secret.replaceWith(mask);
            }
        }
    });
});

document.querySelectorAll(".access-toggle").forEach((button) => {
    const editor = document.getElementById(button.getAttribute("aria-controls"));
    button.addEventListener("click", () => {
        editor.hidden = !editor.hidden;
        button.setAttribute("aria-expanded", String(!editor.hidden));
        button.textContent = editor.hidden ? "Bearbeiten" : "Schließen";
    });
});
