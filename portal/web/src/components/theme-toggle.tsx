import { Monitor, Moon, Sun } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useTheme, type Theme } from "@/hooks/use-theme";

/**
 * @param iconOnly drops the label and current value, for the collapsed
 *   sidebar — 56px has no room for "Theme  SYSTEM", and dropping the control
 *   entirely would mean the theme could only be changed while expanded.
 */
export function ThemeToggle({ iconOnly = false }: { iconOnly?: boolean }) {
  const { theme, resolved, setTheme } = useTheme();
  const Icon = resolved === "dark" ? Moon : Sun;
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          size="sm"
          title={iconOnly ? `Theme: ${theme}` : undefined}
          className={
            iconOnly
              ? "w-full justify-center px-0 text-muted-foreground"
              : "w-full justify-start gap-2 px-2 text-muted-foreground"
          }
        >
          <Icon className="h-4 w-4" />
          {!iconOnly && (
            <>
              <span className="flex-1 text-left">Theme</span>
              <span className="text-xs uppercase tracking-wide">{theme}</span>
            </>
          )}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-44">
        <DropdownMenuLabel>Appearance</DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuRadioGroup
          value={theme}
          onValueChange={(v) => setTheme(v as Theme)}
        >
          <DropdownMenuRadioItem value="system">
            <span className="inline-flex items-center gap-2">
              <Monitor className="h-4 w-4" /> System
            </span>
          </DropdownMenuRadioItem>
          <DropdownMenuRadioItem value="light">
            <span className="inline-flex items-center gap-2">
              <Sun className="h-4 w-4" /> Light
            </span>
          </DropdownMenuRadioItem>
          <DropdownMenuRadioItem value="dark">
            <span className="inline-flex items-center gap-2">
              <Moon className="h-4 w-4" /> Dark
            </span>
          </DropdownMenuRadioItem>
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
