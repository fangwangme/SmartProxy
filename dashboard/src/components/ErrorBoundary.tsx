import { Component, type ErrorInfo, type ReactNode } from 'react'

interface ErrorBoundaryProps {
  children: ReactNode
}

interface ErrorBoundaryState {
  error: Error | null
}

/**
 * Catches render errors in the dashboard tree. Recovery is a state reset rather
 * than a page reload, so a transient bad payload does not cost the whole session.
 */
class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  override state: ErrorBoundaryState = { error: null }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error }
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error('Dashboard render error:', error, info)
  }

  private readonly handleReset = () => {
    this.setState({ error: null })
  }

  override render(): ReactNode {
    const { error } = this.state
    if (!error) return this.props.children

    return (
      <div
        role="alert"
        className="rounded-card border border-red bg-bg1 px-4 py-3 text-sm"
      >
        <p className="font-semibold text-red">Dashboard rendering failed.</p>
        <p className="mt-1 break-words text-fg3">{error.message}</p>
        <button
          type="button"
          onClick={this.handleReset}
          className="mt-3 rounded-md border border-bg3 bg-bg0h px-3 py-1.5 text-sm text-fg1 transition-colors hover:border-bg4 hover:text-fg0"
        >
          Try again
        </button>
      </div>
    )
  }
}

export default ErrorBoundary
