import { Component, type ComponentChildren } from "preact";
import { AlertTriangle, RefreshCw } from "lucide-preact";

export class ErrorBoundary extends Component<{ children: ComponentChildren }, { error?: Error }> {
  state: { error?: Error } = {};

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  render() {
    if (!this.state.error) return this.props.children;
    return <main class="fatal-error">
      <AlertTriangle size={28} />
      <h1>The workspace could not be rendered</h1>
      <p>{this.state.error.message}</p>
      <button class="primary-button" onClick={() => window.location.reload()}><RefreshCw size={16} /> Reload</button>
    </main>;
  }
}
