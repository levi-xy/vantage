import {
  changelogUrl,
  commitsMismatch,
  isKnownCommit,
  RELEASES_URL,
  shownVersion,
} from "./version";

const A = "a".repeat(40);
const B = "b".repeat(40);

describe("versions", () => {
  it("shows the server's release version, else the web app's own", () => {
    expect(shownVersion("0.1.1", "0.1.1-rc.1")).toBe("0.1.1");
    expect(shownVersion(undefined, "0.1.1-rc.1")).toBe("0.1.1-rc.1");
    expect(shownVersion("", "0.1.1-rc.1")).toBe("0.1.1-rc.1");
  });

  it("compares commits, and only known ones", () => {
    expect(commitsMismatch(A, A)).toBe(false);
    expect(commitsMismatch(A, B)).toBe(true);
    for (const unknown of ["", "dev", null, undefined]) {
      expect(isKnownCommit(unknown)).toBe(false);
      expect(commitsMismatch(A, unknown)).toBe(false);
      expect(commitsMismatch(typeof unknown === "string" ? unknown : "", B)).toBe(false);
    }
  });

  it("links the release tag's changelog", () => {
    expect(changelogUrl("0.1.1")).toBe(
      "https://github.com/levi-xy/vantage/blob/v0.1.1/CHANGELOG.md#v0.1.1",
    );
    expect(changelogUrl("0.1.1-rc.2")).toBe(
      "https://github.com/levi-xy/vantage/blob/v0.1.1-rc.2/CHANGELOG.md#v0.1.1",
    );
    expect(changelogUrl("dev")).toBe(RELEASES_URL);
    expect(changelogUrl("0.1.1/../x")).toBe(RELEASES_URL);
  });
});
