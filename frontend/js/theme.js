// Theme + density part. The server stamps data-theme/data-density into <html> at render
// time, so the page paints in the right theme before any JS runs; initTheme() syncs state
// and falls back to the localStorage mirror when no stamp is present.
const themePart = {
  THEMES: [
    { id: "daylight", label: "Daylight", dark: false, swatch: "#ffffff" },
    { id: "parchment", label: "Parchment", dark: false, swatch: "#faf8f4" },
    { id: "slate", label: "Slate", dark: false, swatch: "#f4f6f8" },
    { id: "lilac", label: "Lilac", dark: false, swatch: "#f6f4f8" },
    { id: "ember", label: "Ember", dark: true, swatch: "#1c1917" },
    { id: "midnight", label: "Midnight", dark: true, swatch: "#111827" },
    { id: "pine", label: "Pine", dark: true, swatch: "#0f1a17" },
    { id: "obsidian", label: "Obsidian", dark: true, swatch: "#000000" },
    { id: "system", label: "System", dark: false, swatch: "linear-gradient(90deg, #ffffff 50%, #1c1917 50%)",
      title: "Follow the OS between your light and dark themes" },
  ],
  DENSITIES: ["compact", "normal", "comfortable"],
  theme: "system", density: "normal", lightTheme: "daylight", darkTheme: "ember",

  _themeIsDark(id) {
    const t = this.THEMES.find((x) => x.id === id);
    return t ? t.dark : false;
  },
  themeIsDark(id) {
    const t = id || this.theme;
    if (t === "system") return window.matchMedia("(prefers-color-scheme: dark)").matches;
    return this._themeIsDark(t);
  },

  applyTheme(id) {
    this.theme = id;
    // "system" means the user's remembered light/dark pair following the OS. Stamping the
    // resolved theme (not "system") is what makes those slots take effect - the CSS
    // prefers-color-scheme block only covers the paint before JS runs.
    const effective = id === "system"
      ? (window.matchMedia("(prefers-color-scheme: dark)").matches ? this.darkTheme : this.lightTheme)
      : id;
    document.documentElement.dataset.theme = effective;
    document.documentElement.classList.toggle("dark", this._themeIsDark(effective));
    localStorage.setItem("kl-theme", id);
  },

  // Explicit pick (picker or quick switch). Explicit themes also refresh the remembered
  // light/dark slot the nav quick switch flips between; "system" leaves both as they are.
  setTheme(id) {
    this.applyTheme(id);
    const patch = { theme: id };
    if (id !== "system") {
      if (this.themeIsDark(id)) { this.darkTheme = id; patch.dark_theme = id; }
      else { this.lightTheme = id; patch.light_theme = id; }
    }
    this.klSet(patch);
  },

  quickTheme() { this.setTheme(this.themeIsDark() ? this.lightTheme : this.darkTheme); },

  setDensity(d) {
    this.density = d;
    document.documentElement.dataset.density = d;
    localStorage.setItem("kl-density", d);
    this.klSet({ density: d });
  },

  initTheme(kl) {
    const el = document.documentElement;
    const valid = (x) => this.THEMES.some((t) => t.id === x);
    const stored = localStorage.getItem("kl-theme");
    const id = valid(el.dataset.theme) ? el.dataset.theme
      : valid(stored) ? stored
      : localStorage.getItem("kl-dark") === "1" ? "ember"   // one-time kl-dark migration
      : "system";
    this.lightTheme = valid(kl.light_theme) ? kl.light_theme : "daylight";
    this.darkTheme = valid(kl.dark_theme) ? kl.dark_theme : "ember";
    this.applyTheme(id);
    // While on System, an OS light/dark flip re-resolves immediately.
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
      if (this.theme === "system") this.applyTheme("system");
    });
    this.density = this.DENSITIES.includes(kl.density) ? kl.density
      : this.DENSITIES.includes(localStorage.getItem("kl-density")) ? localStorage.getItem("kl-density")
      : "normal";
    el.dataset.density = this.density;
  },
};
