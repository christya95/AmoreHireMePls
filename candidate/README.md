# Your folder

This folder holds everything the app knows about you. Nothing in it is ever added to git except this note, the
`.gitignore` and `profile.example.toml` (a made up person), so your profile, resume and notes stay on your own computer.

## What goes here

1. **`profile.toml`: who you are and what you want.** Copy `profile.example.toml` to `profile.toml` and replace every
   value. It is written for a fictional enterprise sales account executive in the UK so you can see what each section is
   for; a developer, a psychologist or a project manager fills in the same sections with their own words. The comments
   in the example explain each one: your profession, the facts forms ask for, eligibility, your jobs with dates,
   education, answers you always give, skills, roles and places you want, pay, employers and people to skip, how your
   recruiter notes should sound, resume rules, what counts as a gap in your field, and job boards to watch.
2. **Your base resume as a `.docx` file.** This is the one resume every tailored version starts from. The app keeps its
   layout, header and fonts exactly as they are and only rewrites the summary, the skills lines and the bullet points.
   Use whatever headings your field uses; the common ones are recognised and you can map your own under
   `[resume.section_aliases]`. Headings the app does not know (Languages, Volunteer Work, Publications…) are kept word
   for word. Name the file anything you like, ideally with "resume" or "CV" in the name.
3. **Extra career documents** as `.md`, `.txt` or `.docx` files. Write as much detail as you want about what you actually
   did: projects, cases, deals, numbers, tools, team size, outcomes. The more real detail there is, the more the
   tailored resume has to work with.

Keep one resume `.docx` here. If there are several, the app uses the one whose name contains "resume" or "cv"
(alphabetically first) and tells you which one it picked. Any other `.docx` that does not look like a resume counts as an
extra document.

## How the app uses these files

- **`profile.toml`.** Nothing happens until you load it: press **Load profile file** on the Profile page or run
  `uv run amore profile import --from candidate/profile.toml`. The card on the Profile page shows whether the file is
  new, loaded, or changed since the last load. Sections present in the file replace what was saved; sections you leave
  out keep their saved value, so you can also edit everything directly on the Profile page. `uv run amore profile export
  --to candidate/profile.toml` writes your current settings back into the file. Loading never prints your values, only
  counts.
- **Base resume.** On the Profile page the app shows the resume it found here. Press the button to make it your approved
  base resume. The app copies it into its private folder and records its fingerprint. If you later replace the file, the
  Profile page shows that the folder copy differs and lets you adopt the new one. It never swaps your approved resume
  behind your back.
- **Extra documents.** Their text is read every time the app checks how well a job fits you and every time it tailors
  your resume. It is treated as evidence you wrote yourself: a rewritten bullet may use a fact from these notes, but every
  bullet still comes from a line on your base resume, and the app never invents experience, tools or numbers that appear
  in neither place.
- **Size.** Up to about 12,000 characters of extra text are read (roughly four pages) unless you raise the limit. Files
  are read in alphabetical order, so name the most important one so it sorts first (`01_career_2023_2026.md`). Longer
  text is cut at the limit and the app notes that it was.

## Good to know

- Only files directly in this folder are read. Hidden files, this README and the example are skipped.
- If the app runs on another machine (a NAS), you cannot reach its folder directly. Use "Add a resume or career notes" on
  the Profile page instead: it takes `.docx`, `.md` or `.txt` files up to 5 MB and puts them in that machine's folder.
  For `profile.toml` on that machine, fill in the rules form on the Profile page, or copy the file into the mounted
  candidate folder and press Load.
- You can point the app at a different folder with `AMORE_CANDIDATE_DIR` and change the text limit with
  `AMORE_CANDIDATE_SUPPLEMENTAL_MAX_CHARS` in `.env`.
- Do not put passwords, ID numbers or anything you would not want in a resume in these files. Facts from `profile.toml`
  are typed into forms you approve; the resume and notes are sent to the AI model when it tailors a resume or ranks a job.
