import { graphql } from "@octokit/graphql";

export async function whoAmI(token: string) {
  const { viewer } = await graphql(
    `query WhoAmI { viewer { login name } }`,
    { headers: { authorization: `token ${token}` } },
  );
  return viewer;
}
