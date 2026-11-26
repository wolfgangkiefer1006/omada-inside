const syncForm = document.querySelector("#network-sync-form");
const syncButton = syncForm.querySelector("button");
syncForm.addEventListener("submit", () => {
    syncButton.disabled = true;
    syncButton.textContent = "Abgleich läuft …";
});

function hidePassword(password) {
    password.querySelector(".revealed-secret")?.remove();
    password.querySelector(".password-mask").hidden = false;
    password.querySelector(".credential-hide").hidden = true;
    const reveal = password.querySelector(".credential-reveal");
    reveal.hidden = false;
    return reveal.querySelector("button");
}

document.querySelectorAll(".credential-hide:not([hidden])").forEach((link) => {
    const password = link.closest(".credential-password");
    const item = link.closest(".network-item");
    link.addEventListener("click", (event) => {
        event.preventDefault();
        hidePassword(password).focus();
    });
    item.addEventListener("toggle", () => {
        if (!item.open) hidePassword(password);
    });
    window.addEventListener("pagehide", () => hidePassword(password));
});

document.querySelectorAll(".access-toggle").forEach((button) => {
    const editor = document.getElementById(button.getAttribute("aria-controls"));
    button.addEventListener("click", () => {
        editor.hidden = !editor.hidden;
        button.setAttribute("aria-expanded", String(!editor.hidden));
        button.textContent = editor.hidden ? "Bearbeiten" : "Schließen";
    });
});
