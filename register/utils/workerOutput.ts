const CHILD_LOG_PREFIX = /^\[(\d{2}:\d{2}:\d{2})\]\s+\[(INFO|WARN|ERROR|DEBUG)\]\s*(.*)$/i;

export function formatWorkerProgressLine(scope: string, rawLine: string): string {
  let line = rawLine.trim();
  if (!line) {
    return '';
  }

  let stderrPrefix = '';
  if (line.startsWith('[stderr] ')) {
    stderrPrefix = '[stderr] ';
    line = line.slice('[stderr] '.length).trim();
  }

  let levelPrefix = '';
  const matched = line.match(CHILD_LOG_PREFIX);
  if (matched) {
    const [, , level, message] = matched;
    line = message.trim();
    if (level.toLowerCase() !== 'info') {
      levelPrefix = `[${level.toLowerCase()}] `;
    }
  }

  return `${stderrPrefix}${levelPrefix}[${scope}] ${line}`.trim();
}
