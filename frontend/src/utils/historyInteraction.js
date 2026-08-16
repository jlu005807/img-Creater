export function createHistoryInteractionGuard() {
  let generation = 0

  return {
    begin() {
      generation += 1
      return generation
    },
    invalidate() {
      generation += 1
    },
    isCurrent(token) {
      return token === generation
    },
  }
}
