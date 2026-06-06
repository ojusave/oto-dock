// Voice mode: turn the growing assistant reply into complete, speakable
// sentences as it streams — never starting mid-token, never reading inside an
// unterminated ``` code fence, never re-speaking. Pure + framework-free so it's
// easy to reason about (and smoke-test outside React).
//
// Model: the caller keeps a `cursor` (chars of the reply already consumed) and,
// each time the reply grows, calls extractFlushableSentences(full, cursor) to get
// the next ready sentences + the advanced cursor. On stream completion it calls
// once more with final=true to flush the trailing fragment.

import { cleanForSpeech } from './cleanText'

export interface FlushResult {
  sentences: string[]   // cleaned, ready-to-speak (markdown stripped, empties dropped)
  cursor: number        // advance the caller's cursor to here
}

// A boundary ends a speakable unit: a run of sentence terminators, OR a newline
// run (paragraph / list-item / heading break, for incremental flushing). Trailing
// horizontal whitespace is consumed with it. Applied ONLY to prose segments
// (never across a code fence — fences are skipped whole), so cleanForSpeech still
// sees complete ```...``` blocks and strips them.
const BOUNDARY = /([.!?…]+|\n+)[ \t]*/g

interface Span { text: string; end: number; complete: boolean }

function splitSpans(text: string): Span[] {
  const spans: Span[] = []
  const re = new RegExp(BOUNDARY)
  let last = 0
  let m: RegExpExecArray | null
  while ((m = re.exec(text)) !== null) {
    const end = m.index + m[0].length
    spans.push({ text: text.slice(last, end), end, complete: true })
    last = end
    if (re.lastIndex <= m.index) re.lastIndex = m.index + 1  // never loop on a zero-width match
  }
  // Trailing text past the last boundary has no terminator yet — incomplete.
  if (last < text.length) spans.push({ text: text.slice(last), end: text.length, complete: false })
  return spans
}

export function extractFlushableSentences(full: string, cursor: number, final = false): FlushResult {
  const rem = full.slice(cursor)

  // Never cross an UNTERMINATED code fence: an odd count of ``` means the last
  // one opens a fence still being written → only the text before it is safe.
  // Within [0, safeEnd) every fence is balanced (a complete ```…``` block).
  const fenceCount = rem.split('```').length - 1
  const safeEnd = fenceCount % 2 === 1 ? rem.lastIndexOf('```') : rem.length

  const sentences: string[] = []
  let consumed = 0
  let i = 0

  while (i < safeEnd) {
    const fenceAt = rem.indexOf('```', i)
    const proseEnd = (fenceAt === -1 || fenceAt >= safeEnd) ? safeEnd : fenceAt

    // 1. Prose between fences → sentences (with correct absolute offsets).
    for (const span of splitSpans(rem.slice(i, proseEnd))) {
      const absEnd = i + span.end
      const followed = absEnd < rem.length            // any text after it in the remainder
      const confirmed = span.complete && followed     // boundary confirmed by trailing text
      if (!confirmed && !final) {
        return { sentences, cursor: cursor + consumed }  // hold the trailing fragment
      }
      const c = cleanForSpeech(span.text)
      if (c) sentences.push(c)
      consumed = absEnd
    }
    i = proseEnd

    // 2. Skip a complete fenced block whole (speak nothing for code).
    if (i < safeEnd && rem.startsWith('```', i)) {
      const close = rem.indexOf('```', i + 3)
      i = close === -1 ? safeEnd : close + 3
      consumed = i
    }
  }

  return { sentences, cursor: cursor + consumed }
}
