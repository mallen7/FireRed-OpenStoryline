# Studio-export-only quick start

This is the supported public workflow and requires no proprietary SDK.

1. Open the recording in Insta360 Studio.
2. Apply the desired orientation, FlowState/stabilization, and direction lock.
3. Export one ordinary MP4 containing a stitched equirectangular panorama. The
   frame must be approximately 2:1 with even dimensions. Preserve audio if it
   is needed downstream. H.264 and a constant frame rate are the most-tested
   release path.
4. Start OpenStoryline with `spherical.enabled=true` and
   `enable_sdk_worker=false`.
5. Upload the MP4 in the conversation and ask the agent to import it as a
   stitched spherical asset. The MCP sequence is:

   ```text
   import_spherical_media -> inspect_spherical_media
   -> create_virtual_camera_track or analyze_spherical_asset
   -> preview_virtual_camera_track -> render_reframed_clip
   ```

6. Open the `360°` workspace. Confirm the badge says `360° spherical`, the
   stitch status says `studio_export`, and the displayed source/proxy metadata
   matches the export.
7. Select or create a camera track. Scrub the source and edit timestamp, yaw,
   pitch, roll, FOV, easing, and optional subject. Preview around the playhead,
   then render the approved flat MP4.
8. The final render is copied into the conversation's ordinary media library.
   Continue with the normal OpenStoryline timeline, script, subtitle, BGM, and
   render nodes. Normal nodes never receive the 2:1 master or `.insv` input.

The panorama viewer is for review and camera planning. The deterministic FFmpeg
renderer, not the browser, creates the final flat clip. A warning that raw
`.insv` was not decoded is correct for this workflow.
