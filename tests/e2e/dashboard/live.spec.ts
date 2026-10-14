import { test, expect, type BrowserContext } from "@playwright/test";
import { readFileSync } from "node:fs";

interface Fixture {cookies: Parameters<BrowserContext["addCookies"]>[0]; memberCookies: Parameters<BrowserContext["addCookies"]>[0]; administratorCookies: Parameters<BrowserContext["addCookies"]>[0]; memberId: string; prompt: string; search: string}
const fixture = JSON.parse(readFileSync("live-fixture.json", "utf8")) as Fixture;

test("real Pengolo generation survives browser reload", async ({page,context,browser}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await context.addCookies(fixture.cookies);
  await page.goto("/pengolo");
  await expect(page.getByLabel("Model", {exact:true})).toHaveValue("fixture");
  await page.locator('input[type="file"]').setInputFiles({name:"Evidence.txt",mimeType:"text/plain",buffer:Buffer.from("Confidential operational evidence for the authorized library.")});
  await expect(page.locator(".p-composer")).toContainText("Evidence.txt");
  await page.getByRole("button",{name:"Add files and tools",exact:true}).click();
  await page.getByRole("menuitem",{name:"Galadril Graph Context",exact:true}).click();
  await page.getByLabel("Find a Galadril object",{exact:true}).fill(fixture.search);
  await page.locator(".p-context-results button").first().click();
  await page.getByLabel("Message Pengolo",{exact:true}).fill(fixture.prompt);
  await page.getByRole("button",{name:"Send message",exact:true}).click();
  await expect(page).toHaveURL(/\/pengolo\/[a-f0-9]{32}$/);
  await expect(page.getByRole("button",{name:"Stop generation",exact:true})).toBeVisible();
  const id = page.url();
  await page.reload();
  await expect(page).toHaveURL(id);
  await expect(page.getByRole("article",{name:"Pengolo response"})).toContainText('"reasoning_effort":"medium"',{timeout:90_000});
  await expect(page.getByRole("button",{name:"Stop generation",exact:true})).not.toBeVisible();
  await page.setViewportSize({width:390,height:740});
  await expect(page.getByRole("button",{name:"Open Pengolo sidebar",exact:true})).toBeVisible();
  await page.setViewportSize({width:1440,height:1000});
  await page.getByRole("button",{name:"Open Pengolo sidebar",exact:true}).click();
  await expect(page.locator(".p-sidebar")).not.toHaveClass(/closed/);
  await page.getByRole("button",{name:"Create project",exact:true}).click();
  const projectDialog = page.getByRole("dialog",{name:"Create project",exact:true});
  await projectDialog.getByLabel("Project name",{exact:true}).fill("Browser shared investigation");
  await projectDialog.getByRole("combobox",{name:/^Access/}).selectOption("true");
  await projectDialog.getByRole("textbox",{name:/^Roles/}).fill("pengolo_reader");
  await projectDialog.getByRole("button",{name:"Save project",exact:true}).click();
  const project = page.locator(".p-project").filter({hasText:"Browser shared investigation"});
  await project.locator("summary").click();
  await project.getByRole("link",{name:"Library for Browser shared investigation",exact:true}).click();
  await page.getByRole("region",{name:"File library",exact:true}).locator('input[type="file"]').setInputFiles({name:"Shared evidence.txt",mimeType:"text/plain",buffer:Buffer.from("Evidence shared only with the authorized project role.")});
  await expect(page.getByText("Shared evidence.txt",{exact:true})).toBeVisible();
  if (!await project.getByRole("button",{name:"New chat in Browser shared investigation",exact:true}).isVisible()) await project.locator("summary").click();
  await project.getByRole("button",{name:"New chat in Browser shared investigation",exact:true}).click();
  await page.getByRole("button",{name:"Add files and tools",exact:true}).click();
  await page.getByRole("menuitem",{name:"Choose from library",exact:true}).click();
  await page.getByRole("dialog",{name:"Attach from library",exact:true}).getByRole("button",{name:"Attach",exact:true}).click();
  const sharedPlan:unknown=JSON.parse(fixture.prompt);
  if (typeof sharedPlan!=="object" || sharedPlan===null) throw new Error("Invalid fixture plan");
  await page.getByLabel("Message Pengolo",{exact:true}).fill(JSON.stringify({...sharedPlan,calculator_only:true}));
  await page.getByRole("button",{name:"Send message",exact:true}).click();
  await expect(page).toHaveURL(/\/pengolo\/[a-f0-9]{32}$/);
  await expect(page.getByRole("article",{name:"Pengolo response"})).toContainText('"reasoning_effort":"medium"',{timeout:90_000});
  const sharedUrl=page.url();
  const administratorContext=await browser.newContext();
  await administratorContext.addCookies(fixture.administratorCookies);
  const memberContext=await browser.newContext();
  try {
    await memberContext.addCookies(fixture.memberCookies);
    const member=await memberContext.newPage();
    await member.goto("/pengolo");
    await expect(member.locator(".p-project").filter({hasText:"Browser shared investigation"})).toHaveCount(0);
    for (const forbiddenUrl of [id,sharedUrl]) {
      await member.goto(forbiddenUrl);
      await expect(member.getByRole("alert")).toContainText("no longer available");
      await expect(member.getByRole("article",{name:"Pengolo response"})).toHaveCount(0);
    }
    await member.goto("/pengolo");
    const changeRole=async (operation:string) => {
      const response=await administratorContext.request.post(new URL("/graphql",page.url()).href,{headers:{origin:new URL(page.url()).origin},data:{query:`mutation { administration { ${operation}(userId:"${fixture.memberId}",roleName:"pengolo_reader") } }`}});
      expect(response.status()).toBe(200);
      const result:unknown=await response.json();
      expect(result).not.toHaveProperty("errors");
    };
    await changeRole("assignRoleToUser");
    await member.bringToFront();
    const memberProject=member.locator(".p-project").filter({hasText:"Browser shared investigation"});
    await expect(memberProject).toBeVisible({timeout:25_000});
    await memberProject.locator("summary").click();
    await expect(memberProject.locator(".p-chat-link")).toHaveCount(1);
    await memberProject.locator(".p-chat-link").click();
    await expect(member.getByRole("article",{name:"Pengolo response"})).toContainText('"reasoning_effort":"medium"');
    if (!await memberProject.getByRole("link",{name:"Library for Browser shared investigation",exact:true}).isVisible()) await memberProject.locator("summary").click();
    await memberProject.getByRole("link",{name:"Library for Browser shared investigation",exact:true}).click();
    await expect(member.getByText("Shared evidence.txt",{exact:true})).toBeVisible();
    await member.getByRole("button", {name:"Preview Shared evidence.txt", exact:true}).click();
    await expect(member.getByRole("dialog", {name:"Shared evidence.txt", exact:true})).toBeVisible();
    await changeRole("unassignRoleFromUser");
    await member.bringToFront();
    await expect(memberProject).toHaveCount(0,{timeout:25_000});
    await expect(member.getByRole("dialog", {name:"Shared evidence.txt", exact:true})).toHaveCount(0);

    await expect(member.getByText("Shared evidence.txt",{exact:true})).toHaveCount(0);
    await member.goto(sharedUrl);
    await expect(member.getByRole("alert")).toContainText("no longer available");
  } finally { await Promise.all([memberContext.close(),administratorContext.close()]); }
  expect(errors).toEqual([]);
});
