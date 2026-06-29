# AI Video Generator SaaS – Master Software Requirements Document (SRD)

> **THIS IS THE GOVERNING RULE FILE FOR THIS PROJECT.**
> All work performed inside the `ai creation/` folder MUST strictly conform to this document.
> Do NOT hallucinate features, libraries, file names, or APIs. If a detail is not specified here, ASK before assuming.
> Every module produced MUST be production-ready. Placeholder code is FORBIDDEN unless explicitly requested.

---

## ROLE

You are simultaneously acting as:

- Principal Software Architect
- Senior Full Stack Engineer
- AI Engineer
- UI/UX Designer
- DevOps Engineer
- Product Manager

Your responsibility is to design and build a production-ready AI-powered Video Generation platform comparable to InVideo AI, Canva AI, Pika, and Runway.

The application must follow enterprise-grade software engineering practices and be suitable for commercial deployment.

- Never generate placeholder code unless explicitly instructed.
- Every module must be production-ready.

---

## OBJECTIVE

Build an AI-powered SaaS platform that transforms any text prompt or complete script into a professional cinematic video.

The platform should automatically:

- Understand the script
- Analyze context
- Generate storyboard
- Split scenes
- Generate AI prompts
- Generate images
- Generate AI videos
- Generate narration
- Generate subtitles
- Select music
- Apply transitions
- Render
- Export

The application should require minimal manual editing.

---

## SOFTWARE ENGINEERING RULES

Follow:

- Clean Architecture
- SOLID Principles
- Repository Pattern
- Factory Pattern
- Strategy Pattern
- Provider Pattern
- Dependency Injection
- Domain Driven Design
- Modular Design
- Microservice-friendly Architecture

The project must be scalable enough to support millions of users.

---

## TECHNOLOGY STACK

### Frontend

- Next.js 15
- React
- TypeScript
- TailwindCSS
- ShadCN UI
- React Query
- Zustand
- Framer Motion
- React Hook Form
- Zod

### Backend

- Python
- FastAPI
- SQLAlchemy
- Alembic
- Celery
- Redis
- PostgreSQL
- JWT Authentication
- OAuth
- WebSockets

### AI Framework

- LangGraph
- LangChain
- CrewAI
- AutoGen
- OpenAI SDK
- Anthropic SDK
- Google Gemini SDK

### Image Generation Providers

- FLUX
- SDXL
- Stable Diffusion XL
- ComfyUI
- Ideogram
- DALL-E

### Video Providers

- Google Veo
- Runway
- Kling AI
- Pika
- Luma
- Stable Video Diffusion
- ComfyUI

### Voice Providers

- ElevenLabs
- XTTS
- OpenAI TTS
- Edge TTS
- Coqui

### Rendering

- FFmpeg
- MoviePy
- OpenCV

### Storage

- AWS S3
- Cloudflare R2
- Local Storage

---

## USER FLOW

User enters:

- Topic
- Script
- Prompt
- Style
- Duration
- Language
- Aspect Ratio

Then the pipeline runs:

AI analyzes script → Creates storyboard → Splits scenes → Generates prompts → Generates media → Generates narration → Generates subtitles → Adds music → Creates timeline → Renders final video → Export.

---

## CORE MODULES

### Authentication

- Register
- Login
- OAuth
- Google Login
- Password Reset
- Email Verification
- JWT
- User Roles

### Dashboard

- Projects
- Templates
- Analytics
- Credits
- Usage
- Recent Projects

### Project Manager

- Create Project
- Duplicate
- Delete
- Folders
- Search
- Tags
- Autosave

### AI Script Generator

- Long-form scripts
- Shorts
- Reels
- Documentary
- Storytelling
- Educational
- Promotional
- Podcast
- Product Review

### Script Analyzer

Extract: People, Objects, Animals, Locations, Weather, Time, Emotion, Tone, Camera Style, Lighting, Cinematic Style, Keywords. Output structured JSON.

### Storyboard Generator

Each storyboard contains: Scene Number, Scene Title, Duration, Narration, Subtitle, Prompt, Negative Prompt, Emotion, Camera Angle, Camera Motion, Lens, Lighting, Weather, Location, Animation, Transition, Sound Effects, Music Mood.

### Scene Generator

Automatically split script into intelligent scenes. Every scene should contain complete metadata.

### Prompt Generator

Generate: Image Prompt, Video Prompt, Animation Prompt, Negative Prompt, Camera Prompt, Motion Prompt, Lighting Prompt, Style Prompt.

### AI Image Generator

Generate, Regenerate, Variation, Upscale, Face Consistency, Character Consistency, Reference Images.

### AI Video Generator

Generate, Image-to-Video, Text-to-Video, Extend Clip, Replace Clip, Motion Control, Camera Control, Consistency.

### Voice Generator

Multiple Voices, Emotion, Pitch, Speed, Voice Cloning, Multiple Languages.

### Subtitle Generator

Automatic Timing, Word Timing, Speaker Detection, Translation, Burn Captions.

### Timeline Editor

Professional editing timeline. Drag, Drop, Split, Trim, Merge, Replace, Move, Lock, Zoom, Undo, Redo.

### Music

AI Music Recommendation, Royalty Free Music, Auto Sync, Fade, Volume Control.

### Sound Effects

Auto generation of: Rain, Explosion, Wind, Typing, Footsteps, Crowd, Nature, Vehicles.

### Rendering Engine

Combine: Images, Videos, Voice, Subtitles, Music, Transitions, Animations. Render using FFmpeg.

### Export

- Formats: MP4, MOV, GIF, WebM
- Quality: 1080P, 2K, 4K
- Orientation: Vertical, Horizontal, Square

---

## AI AGENTS

Create independent agents:

- **Script Agent** – Writes scripts.
- **Analysis Agent** – Analyzes script.
- **Storyboard Agent** – Creates storyboard.
- **Prompt Agent** – Creates prompts.
- **Voice Agent** – Creates narration.
- **Subtitle Agent** – Creates subtitles.
- **Image Agent** – Creates images.
- **Video Agent** – Creates videos.
- **Render Agent** – Builds final timeline.
- **SEO Agent** – Creates Title, Description, Keywords, Hashtags, Thumbnail Text.

---

## DATABASE

Design complete PostgreSQL schema. Include:

- Users
- Projects
- Assets
- Scenes
- Videos
- Templates
- Credits
- Billing
- Logs
- Settings
- Analytics
- Notifications

---

## API DESIGN

- Design REST APIs.
- Document every endpoint.
- Return consistent JSON.
- Implement versioning.
- Use OpenAPI.

---

## UI

- Modern dark theme.
- Apple-quality interface.
- Smooth animations.
- Responsive.
- Professional dashboard.
- Sidebar, Timeline, Editor, Preview, Settings, Export, Billing, Analytics.

---

## DEVOPS

- Docker
- Docker Compose
- GitHub Actions
- CI/CD
- Environment Variables
- Logging
- Monitoring
- Health Checks
- Production Configuration

---

## TESTING

- Unit Tests
- Integration Tests
- API Tests
- UI Tests
- Performance Tests

---

## DOCUMENTATION

Generate:

- README
- Installation Guide
- Architecture Diagram
- Database Diagram
- Deployment Guide
- API Documentation
- Developer Guide

---

## DEVELOPMENT PROCESS (PHASED — STRICT)

**Never generate the whole project at once.** Follow this exact order:

1. **Phase 1** – Architecture, Folder Structure, Tech Decisions
2. **Phase 2** – Database
3. **Phase 3** – Authentication
4. **Phase 4** – Backend APIs
5. **Phase 5** – Frontend
6. **Phase 6** – AI Pipeline
7. **Phase 7** – Timeline
8. **Phase 8** – Rendering
9. **Phase 9** – Deployment
10. **Phase 10** – Testing

> **PAUSE after every phase. WAIT for explicit user approval before continuing to the next phase.**

---

## CODING STANDARDS

- Use meaningful names.
- Avoid duplicate code.
- Write reusable components.
- Use TypeScript everywhere possible.
- Use async/await.
- Handle all errors.
- Add logging.
- Write comments where necessary (intent, not narration).
- Optimize performance.
- Follow security best practices.
- Never use hardcoded secrets.
- Never leave TODOs.
- Every generated module must be production-ready.

---

## FINAL GOAL

The completed project should be capable of competing with:

- InVideo AI
- Canva AI
- Runway
- Pika
- Kling AI

The architecture must be extensible so new AI providers can be added with minimal changes using a Provider Pattern.

---

## ANTI-HALLUCINATION GUARDRAILS

When working inside `ai creation/`:

1. **Never invent dependencies.** Only use the libraries listed in the Technology Stack section. If something else is needed, ASK first.
2. **Never invent file paths.** Confirm folder structure against `ARCHITECTURE.md` (produced in Phase 1) before referencing any path.
3. **Never invent API contracts.** All endpoints, schemas, and DTOs must be defined in `ARCHITECTURE.md` / `API.md` before being implemented.
4. **Never skip phases.** Phase N+1 cannot begin until Phase N is explicitly approved by the user.
5. **Never produce placeholder code** (no `pass`, no `TODO`, no `# implement later`, no stubbed mocks) unless the user explicitly says so.
6. **Cite the source** when a design decision is made — point at the exact section of this `rule.md` it derives from.
7. **If uncertain, ask.** Do not guess provider SDK signatures, environment variable names, or schema fields.

---

**Begin with Phase 1: Architecture and Folder Structure. Do not generate any implementation code until the architecture has been reviewed and approved.**
