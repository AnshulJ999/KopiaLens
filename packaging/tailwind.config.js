// Tailwind build config for frontend/css/tailwind.css. Build: python packaging/build_css.py
// The semantic colours point at the theme tokens in frontend/css/tokens.css.
module.exports = {
  darkMode: 'class',
  content: {
    relative: true,
    files: [
      '../frontend/index.html',
      '../frontend/partials/**/*.html',
      '../frontend/js/**/*.js',
    ],
  },
  theme: {
    extend: {
      colors: {
        canvas: 'var(--canvas)', surface: 'var(--surface)', surface2: 'var(--surface2)', head: 'var(--head)',
        sel: 'var(--sel)',
        bd: 'var(--bd)', 'bd-soft': 'var(--bd-soft)',
        fg: 'var(--fg)', muted: 'var(--muted)', faint: 'var(--faint)',
        ok: 'var(--ok)', 'ok-bd': 'var(--ok-bd)', 'ok-bg': 'var(--ok-bg)', 'ok-solid': 'var(--ok-solid)',
        danger: 'var(--danger)', 'danger-bd': 'var(--danger-bd)', 'danger-bg': 'var(--danger-bg)',
        warn: 'var(--warn)', 'warn-bd': 'var(--warn-bd)', 'warn-bg': 'var(--warn-bg)',
        link: 'var(--link)',
        accent: 'var(--accent)', 'accent-bd': 'var(--accent-bd)', 'accent-bg': 'var(--accent-bg)', 'accent-solid': 'var(--accent-solid)',
      },
    },
  },
  plugins: [],
};
