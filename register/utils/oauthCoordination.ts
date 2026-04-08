export async function waitForOauthAndPureLogin(
  oauthResult: Promise<void>,
  pureLoginResult: Promise<Record<string, unknown>>,
): Promise<Record<string, unknown>> {
  const oauthOutcome = oauthResult.then(
    () => ({ source: 'oauth' as const, ok: true as const }),
    (error) => ({ source: 'oauth' as const, ok: false as const, error }),
  );
  const pureOutcome = pureLoginResult.then(
    (summary) => ({ source: 'pure' as const, ok: true as const, summary }),
    (error) => ({ source: 'pure' as const, ok: false as const, error }),
  );

  const first = await Promise.race([oauthOutcome, pureOutcome]);
  if (first.source === 'oauth') {
    if (!first.ok) {
      throw first.error;
    }
    const pure = await pureOutcome;
    if (!pure.ok) {
      throw pure.error;
    }
    return pure.summary;
  }

  if (!first.ok) {
    throw first.error;
  }
  return first.summary;
}
