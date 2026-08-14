/* Icon helper for page modules. Both the symbol sheet and the markup builder are
   installed by the blocking sprite.js script; this only re-exposes the builder so
   module code keeps a normal import. */

export function icon(name, className = "") {
  return window.streamKeeperIcon(name, className);
}
