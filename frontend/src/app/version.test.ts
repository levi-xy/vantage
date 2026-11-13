import { changelogAnchor, slug } from "../../scripts/changelog-anchor.mjs";

const LOG = `# Changelog

## 0.1.10 (2026-12-01)

- x

## 0.1.1 (2026-11-20)

- y

## 0.1.1-rc.1 (2026-11-15)
`;

describe("changelog anchor", () => {
  it("slugs headings the way GitHub does", () => {
    expect(slug("0.1.0 (2026-11-12)")).toBe("010-2026-11-12");
    expect(slug("0.1.1-rc.1 (2026-11-15)")).toBe("011-rc1-2026-11-15");
  });

  it("matches the version as a whole token", () => {
    expect(changelogAnchor(LOG, "0.1.1")).toBe("011-2026-11-20");
    expect(changelogAnchor(LOG, "0.1.1-rc.1")).toBe("011-rc1-2026-11-15");
    expect(changelogAnchor(LOG, "0.1.10")).toBe("0110-2026-12-01");
  });

  it("gives no anchor for dev, empty or unknown versions", () => {
    expect(changelogAnchor(LOG, "dev")).toBe("");
    expect(changelogAnchor(LOG, "")).toBe("");
    expect(changelogAnchor(LOG, "0.2.0")).toBe("");
  });
});
