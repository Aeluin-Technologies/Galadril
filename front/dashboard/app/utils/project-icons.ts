import {
  FolderIcon,
  ChartBarIcon,
  MagnifyingGlassIcon,
  MapIcon,
  GlobeAltIcon,
  ScaleIcon,
  LightBulbIcon,
  BeakerIcon,
  WrenchScrewdriverIcon,
  ArrowTrendingUpIcon,
  BuildingOfficeIcon,
  SignalIcon,
} from "@heroicons/vue/24/outline";
export const projectIcons = [
  { id: "folder", label: "Folder", icon: FolderIcon, legacy: "📁" },
  { id: "chart", label: "Chart", icon: ChartBarIcon, legacy: "📊" },
  { id: "search", label: "Search", icon: MagnifyingGlassIcon, legacy: "🔎" },
  { id: "compass", label: "Compass", icon: MapIcon, legacy: "🧭" },
  { id: "globe", label: "Globe", icon: GlobeAltIcon, legacy: "🌍" },
  { id: "scale", label: "Scale", icon: ScaleIcon, legacy: "⚖️" },
  { id: "idea", label: "Idea", icon: LightBulbIcon, legacy: "💡" },
  { id: "science", label: "Science", icon: BeakerIcon, legacy: "🧪" },
  { id: "tools", label: "Tools", icon: WrenchScrewdriverIcon, legacy: "🛠️" },
  { id: "trend", label: "Trend", icon: ArrowTrendingUpIcon, legacy: "📈" },
  { id: "building", label: "Building", icon: BuildingOfficeIcon, legacy: "🏢" },
  { id: "signal", label: "Signal", icon: SignalIcon, legacy: "🛰️" },
] as const;
/** Legacy stored emojis map to the same reviewed SVG choices without rewriting records. */
export function projectIcon(value: string) {
  return (
    projectIcons.find((item) => item.id === value || item.legacy === value) ??
    projectIcons[0]
  );
}
