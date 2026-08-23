import { render, screen } from "@testing-library/preact";
import { describe, expect, it } from "vitest";
import { Markdown } from "./Markdown";

describe("Markdown", () => {
  it("renders agent content and removes active markup", () => {
    const { container } = render(<Markdown content={'## Result\n\n<script>alert("xss")</script><img src="x" onerror="alert(1)">'} />);
    expect(screen.getByRole("heading", { name: "Result" })).toBeInTheDocument();
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")?.hasAttribute("onerror")).toBe(false);
  });
});
