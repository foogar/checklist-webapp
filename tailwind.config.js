/** Tailwind configuration for the build step.
 *
 * darkMode: 'class' is REQUIRED, not a default. The app has a manual dark mode
 * toggle that sets a class on <html> (see templates/index.html). Tailwind
 * defaults to 'media', which emits prefers-color-scheme queries instead of
 * .dark selectors, and the toggle would silently stop working.
 *
 * Play CDN had this set inline in the page; the built stylesheet has no way to
 * read that, so it lives here.
 */
module.exports = {
  darkMode: 'class',
  content: ["./templates/**/*.html"],
}
