import { Component, type ErrorInfo, type ReactNode } from 'react'

interface Props {
  extensionId: string
  children: ReactNode
  compact?: boolean
}

interface State {
  failed: boolean
}

export class ExtensionBoundary extends Component<Props, State> {
  state: State = { failed: false }

  static getDerivedStateFromError(): State {
    return { failed: true }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(`扩展 ${this.props.extensionId} 渲染失败`, error, info)
  }

  render() {
    if (!this.state.failed) return this.props.children
    if (this.props.compact) return null
    return (
      <div className="rounded border border-danger/30 bg-danger/5 px-3 py-2 text-xs text-danger">
        扩展 {this.props.extensionId} 暂时不可用
      </div>
    )
  }
}
