import type { ThemePreference } from '../hooks/useTheme'
import ThemeToggle from './ThemeToggle'

interface HeaderProps {
  themePreference: ThemePreference
  onThemeChange: (preference: ThemePreference) => void
}

const Header = ({ themePreference, onThemeChange }: HeaderProps) => (
  <header className="mb-4 flex flex-wrap items-start justify-between gap-3">
    <div>
      <h1 className="text-xl font-semibold tracking-tight text-fg0">
        Proxy Service Dashboard
      </h1>
      <p className="mt-0.5 text-xs text-fg4">
        Success rate and request volume per proxy source.
      </p>
    </div>
    <ThemeToggle preference={themePreference} onChange={onThemeChange} />
  </header>
)

export default Header
