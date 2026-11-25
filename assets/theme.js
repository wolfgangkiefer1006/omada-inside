(() => {
    let stored;
    try { stored = localStorage.getItem("omada-theme"); } catch {}
    const theme = ["light", "dark"].includes(stored) ? stored : "system";
    document.documentElement.dataset.theme = theme;
    document.addEventListener("DOMContentLoaded", () => {
        const menu = document.querySelector("#theme");
        menu.value = theme;
        menu.addEventListener("change", () => {
            document.documentElement.dataset.theme = menu.value;
            try { localStorage.setItem("omada-theme", menu.value); } catch {}
        });
    });
})();
