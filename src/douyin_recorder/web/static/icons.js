/* Icon helper. The symbol sheet itself is injected by the blocking sprite.js script. */

export function icon(name, className = "") {
  return `<svg class="ic${className ? ` ${className}` : ""}" aria-hidden="true" focusable="false"><use href="#ic-${name}"/></svg>`;
}
