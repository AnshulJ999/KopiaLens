// Theme + density part. The server stamps data-theme/data-density into <html> at render
// time, so the page paints in the right theme before any JS runs; initTheme() syncs state
// and falls back to the localStorage mirror when no stamp is present.
const themePart = {
  THEMES: [
    { id: "daylight", label: "Daylight", dark: false, swatch: "#ffffff" },
    { id: "parchment", label: "Parchment", dark: false, swatch: "#faf8f4" },
    { id: "slate", label: "Slate", dark: false, swatch: "#f4f6f8" },
    { id: "lilac", label: "Lilac", dark: false, swatch: "#f6f4f8" },
    { id: "kopia", label: "Kopia Blue", dark: false, swatch: "linear-gradient(135deg, #f4f8fe 60%, #2a7fff 60%)" },
    { id: "sky", label: "Sky Blue", dark: false, swatch: "linear-gradient(135deg, #f3fafe 60%, #7dd3fc 60%)" },
    { id: "teal", label: "Teal", dark: false, swatch: "linear-gradient(135deg, #f3faf8 60%, #0f766e 60%)" },
    { id: "ember", label: "Ember", dark: true, swatch: "#1c1917" },
    { id: "midnight", label: "Midnight", dark: true, swatch: "#111827" },
    { id: "obsidian", label: "Obsidian", dark: true, swatch: "#000000" },
    { id: "lens", label: "Lens", dark: true, swatch: "linear-gradient(135deg, #101918 60%, #2dd4bf 60%)" },
    { id: "graphite", label: "Graphite", dark: true, swatch: "#18191c" },
    { id: "ink", label: "Ink", dark: true, swatch: "#11141b" },
    { id: "system", label: "System", dark: false, swatch: "linear-gradient(90deg, #ffffff 50%, #101918 50%)",
      title: "Follow the OS between your light and dark themes" },
  ],
  DENSITIES: ["comfortable", "normal", "compact", "dense"],
  FONTS: [
    { id: "inter", label: "Inter" },
    { id: "geist", label: "Geist" },
    { id: "plex", label: "IBM Plex Sans" },
    { id: "system", label: "System", title: "Your operating system's own font" },
  ],
  DENSITY_PCT: { min: 80, max: 115 },
  theme: "system", density: "normal", densityPct: 100, font: "inter",
  lightTheme: "daylight", darkTheme: "lens", sortArrows: "sorted",

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

  // Row heights change with density, and Tabulator caches them.
  _redrawTables() {
    requestAnimationFrame(() => document.querySelectorAll(".tabulator").forEach((el) => {
      const t = Tabulator.findTable(el);
      if (t && t[0]) t[0].redraw(true);
    }));
  },

  _applyDensity() {
    const el = document.documentElement;
    el.dataset.density = this.density;
    if (this.density === "custom") el.style.setProperty("--kl-density-pct", this.densityPct);
    else el.style.removeProperty("--kl-density-pct");
    this._redrawTables();
  },

  setDensity(d) {
    this.density = d;
    this._applyDensity();
    localStorage.setItem("kl-density", d);
    this.klSet({ density: d });
  },

  // The Settings slider: any size between the presets. Saved once the slider is let go.
  slideDensity(v, save) {
    this.densityPct = Math.round(Math.min(this.DENSITY_PCT.max, Math.max(this.DENSITY_PCT.min, Number(v) || 100)));
    this.density = "custom";
    this._applyDensity();
    if (save) {
      localStorage.setItem("kl-density", "custom");
      this.klSet({ density: "custom", density_pct: this.densityPct });
    }
  },

  setFont(id) {
    this.font = id;
    document.documentElement.dataset.font = id;
    this._redrawTables();
    this.klSet({ font: id });
  },

  // Table header sort arrows: "sorted" shows one only on the sorted column (and on hover).
  setSortArrows(v) {
    this.sortArrows = v === "all" ? "all" : "sorted";
    document.documentElement.dataset.sortarrows = this.sortArrows;
    this.klSet({ sort_arrows: this.sortArrows });
  },

  initTheme(kl) {
    const el = document.documentElement;
    const valid = (x) => this.THEMES.some((t) => t.id === x);
    const moved = (x) => (x === "pine" ? "lens" : x);   // Pine was removed
    const stored = moved(localStorage.getItem("kl-theme"));
    const id = valid(el.dataset.theme) ? el.dataset.theme
      : valid(stored) ? stored
      : localStorage.getItem("kl-dark") === "1" ? "ember"   // one-time kl-dark migration
      : "system";
    this.lightTheme = valid(kl.light_theme) ? kl.light_theme : "daylight";
    this.darkTheme = valid(moved(kl.dark_theme)) ? moved(kl.dark_theme) : "lens";
    this.applyTheme(id);
    // While on System, an OS light/dark flip re-resolves immediately.
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
      if (this.theme === "system") this.applyTheme("system");
    });
    const dens = [...this.DENSITIES, "custom"];
    this.density = dens.includes(kl.density) ? kl.density
      : dens.includes(localStorage.getItem("kl-density")) ? localStorage.getItem("kl-density")
      : "normal";
    const pct = Number(kl.density_pct);
    this.densityPct = pct >= this.DENSITY_PCT.min && pct <= this.DENSITY_PCT.max ? pct : 100;
    el.dataset.density = this.density;
    if (this.density === "custom") el.style.setProperty("--kl-density-pct", this.densityPct);
    this.font = this.FONTS.some((f) => f.id === kl.font) ? kl.font : "inter";
    el.dataset.font = this.font;
    this.sortArrows = kl.sort_arrows === "all" ? "all" : "sorted";
    el.dataset.sortarrows = this.sortArrows;
  },
};
