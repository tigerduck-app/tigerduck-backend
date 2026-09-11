// Platform identity, shared by every page that groups devices.
//
// This lives in lib/ rather than a page's format module because two pages —
// the device population charts and the per-student devices card — bucket the
// same devices and have to agree. When the mapping was copied into both, a new
// platform added to one and missed in the other would have split a family in
// one view and not the other, silently and with no failing test.

export const PLATFORM_LABELS: Record<string, string> = {
  ios: "iOS",
  ipados: "iPadOS",
  macos: "macOS",
  watchos: "watchOS",
  android: "Android",
  wearos: "Wear OS",
  web: "Web",
};

export function platformLabel(p: string) {
  return PLATFORM_LABELS[p] ?? p;
}

/** Platforms that ship from the Apple build. */
export const APPLE_PLATFORMS = new Set(["ios", "ipados", "macos", "watchos"]);

/** Platforms that ship from the Android build. */
export const ANDROID_PLATFORMS = new Set(["android", "wearos"]);

export function isApplePlatform(p: string) {
  return APPLE_PLATFORMS.has(p);
}

/**
 * The build family a platform ships from.
 *
 * The two apps version independently, so an app version only identifies a
 * release together with the family it shipped from — "2.0.1" is a different
 * build on each, and counting the bare string merges them into one slice.
 * Family rather than platform: an iPhone and an iPad run the same Apple build.
 *
 * Anything unrecognised falls back to its own label, so a platform added to
 * the backend before it is classified here shows up as itself rather than
 * being folded into the wrong family.
 */
export function familyLabel(p: string) {
  if (APPLE_PLATFORMS.has(p)) return "Apple";
  if (ANDROID_PLATFORMS.has(p)) return "Android";
  return platformLabel(p);
}
