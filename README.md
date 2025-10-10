## Setup Instructions

### 1. Create a new Mastodon account
1. Get an application access token:
   - Go to **Settings > Development > New Application**
   - Give it the following permissions and save:
     - `profile`
     - `write:media`
     - `write:statuses`
   - Copy the new **Your access token** somewhere handy.

### 2. Create a new GitHub repository (or clone this one)
1. If creating (not cloning), copy the following files to the root of the repo:
   - `LICENSE`
   - `bsky_to_masto.py`
   - `requirements.txt`
2. Create the following directory structure:
   - `.github/workflows`
3. Copy the `crosspost.yml` file into `.github/workflows`

### 3. Create the secrets and variables
1. Go to **Settings** of the repo and navigate to **Secrets and variables** under the **Security** section.
2. Click **Actions**.

#### Repository Secrets
Create the following secrets under the **Secrets** tab:
- `BLUESKY_HANDLE` = Full Bluesky handle (e.g. `@jayisabigot.bsky.social`)
- `MASTODON_ACCESS_TOKEN` = Access token from the Mastodon account created in Step 1
- `MASTODON_BASE_URL` = Base URL of the Mastodon server (e.g. `https://mastodon.social`)

#### Repository Variables
Create the following variables under the **Variables** tab:
- `BLUESKY_FILTER` = `posts_with_media`
- `DRY_RUN` = `false`
- `FETCH_LIMIT` = `20`
- `MASTODON_MAX_CHARS` = `500`
- `MASTODON_VISIBILITY` = `public`
- `REQUIRE_IMAGES` = `true`

## Notes and Troubleshooting
### Notes
It goes without saying that the Bsky account has to post publicly. If they private all/any posts, this script will not mirror them because it has no ability to see private posts.
### Dry Run and Verbose Logging
You can do a dry-run with verbose logging by EITHER doing a manual workflow run and putting "true" in the dry-run and verbose fields OR you can chage the variable from "true" to "false".
Just remember that if you change the variables, to change them back.
When doing a dry-run it'll output in the action log what is going on, but it won't actually post anything.

You can see the verbose logging by clicking on one of the Workflow runs, expanding "crosspost" and expanding "Run crossposter".

Under normal conditions, you shouldn't have verbose set to true. It's really only for debugging.
### Scheduling
The crossposter runs every 10 minutes. You can change that in the .github/workflows/crosspost.yml file by editing the `cron:` line using good old fashion cron style scheduling.

>The workflow actions of github are notoriously unreliable. Since this was created to mirror stupid little image bot accounts from bsky to Mastodon, its failure at being constistant isn't a big deal.
>Also remember that in order for the Workflow Action to actually run, you have to have alla the files in the main default repo. If you stick in a non-default branch of the repo, it will not run.

### Making moar
If you want to have multiple of these mirror thingies, you can just clone your first one to another repo and change the secrets (and remember to make a new masto account).

>When you make a mirror Mastodon account, don't forget to mark it as an automated account! It's also usually good manners to put something in the profile that it's a mirror and link back to the OG bluesky one.

### Variables
You can manipulate some of the variables to do different things:
- `BLUESKY_FILTER` this defaults to "posts_with_media" other options could be "posts_with_video", "posts_with_replies"; etc. This is from [app.bsky.feed.get_author_feed](https://atproto.blue/en/latest/atproto/atproto_client.models.app.bsky.feed.get_author_feed.html). I don't know or care or test anything but "posts_with_media" because this is to pull funny animal pics and shit from bsky. Any other option would require testing on your own.
- `DRY-RUN` toggles dry running the script can be "true" or "false".
- `FETCH_LIMIT` limits requests per run default is 20, bsky api allows up to 100.
- `MASTODON_MAX_CHARS` default is 500 on most instances, this keeps things consistant since non-default instances can have more.
- `MASTODON_VISIBLITY` the post visiblity for the Mastodon posts. Can be "public" or "private".
- `REQUIRES_IMAGES` defaults "true". This is to keep posts out like replies or non-media posts. This was required since the original concept of this mess was to mirror accounts that post images and replies and non-image posts are unwanted.

### Help and Support
LOL, I created this with minimal, shaky python skills, snippets of code bits I stole/found on the web, and my glassy smooth koala brain. Use at your own risk. If you ask me how to change or adapt something, or why something isn't working for you, I will probably not know and/or ignore you.
