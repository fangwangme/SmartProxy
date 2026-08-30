import type { ThemePreference } from '../hooks/useTheme'
import { handleRadioGroupKeys } from '../utils/radioGroupKeys'
import { MonitorIcon, MoonIcon, SunIcon } from './icons'

interface ThemeToggleProps {
  preference: ThemePreference
  onChange: (preference: ThemePreference) => void
}

const OPTIONS: {
  value: ThemePreference
  label: string
  Icon: typeof SunIcon
}[] = [
  { value: 'system', label: 'Follow system theme', Icon: MonitorIcon },
  { value: 'light', label: 'Light theme', Icon: SunIcon },
  { value: 'dark', label: 'Dark theme', Icon: MoonIcon },
]

const ThemeToggle = ({ preference, onChange }: ThemeToggleProps) => (
  <div
    role="radiogroup"
    aria-label="Colour theme"
    onKeyDown={handleRadioGroupKeys}
    className="flex items-center gap-0.5 rounded-md border border-bg3 bg-bg1 p-0.5"
  >
    {OPTIONS.map(({ value, label, Icon }) => {
      const selected = preference === value
      return (
        <button
          key={value}
          type="button"
          role="radio"
          aria-checked={selected}
          tabIndex={selected ? 0 : -1}
          aria-label={label}
          title={label}
          onClick={() => {
            onChange(value)
          }}
          className={`rounded px-2 py-1 text-base transition-colors ${
            selected
              ? 'bg-accent text-bg0'
              : 'text-fg4 hover:bg-bg2 hover:text-fg1'
          }`}
        >
          <Icon />
        </button>
      )
    })}
  </div>
)

export default ThemeToggle
