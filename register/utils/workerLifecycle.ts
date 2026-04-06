export type CancelSessionHandler = (sessionId: string) => Promise<void>;
export type Terminator = () => void;

export class WorkerLifecycleController {
  private activeSessionId: string | null = null;
  private shutdownPromise: Promise<void> | null = null;
  private shuttingDown = false;
  private readonly terminators = new Set<Terminator>();

  trackSession(sessionId: string | null | undefined): void {
    if (!sessionId) {
      return;
    }
    this.activeSessionId = sessionId;
  }

  clearSession(sessionId: string): void {
    if (this.activeSessionId === sessionId) {
      this.activeSessionId = null;
    }
  }

  canSendSessionUpdate(sessionId: string): boolean {
    return !this.shuttingDown && this.activeSessionId === sessionId;
  }

  registerTerminator(terminator: Terminator): () => void {
    this.terminators.add(terminator);
    return () => {
      this.terminators.delete(terminator);
    };
  }

  async shutdown(cancelSession?: CancelSessionHandler): Promise<void> {
    if (this.shutdownPromise) {
      await this.shutdownPromise;
      return;
    }

    const sessionId = this.activeSessionId;
    this.shutdownPromise = (async () => {
      this.shuttingDown = true;
      for (const terminator of [...this.terminators]) {
        try {
          terminator();
        } catch {
          // ignore cleanup errors from local child processes
        }
      }
      this.terminators.clear();
      if (sessionId && cancelSession) {
        await cancelSession(sessionId);
      }
    })();
    await this.shutdownPromise;
  }
}
