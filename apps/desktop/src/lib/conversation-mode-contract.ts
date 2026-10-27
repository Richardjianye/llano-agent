export class ChatModeUnavailableError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'ChatModeUnavailableError'
  }
}

// Work remains compatible with older runtimes. Chat is an explicit capability
// contract: absence of a positive acknowledgement must never enable all tools.
export function requireConversationMode(requested: unknown, confirmed: unknown, message: string): void {
  if (requested === 'chat' && confirmed !== 'chat') {
    throw new ChatModeUnavailableError(message)
  }
}
