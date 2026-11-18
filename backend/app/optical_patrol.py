"""Direction-aware line gates in two independent image coordinate spaces.

One passage keeps its file and locked vessel across multiple zoom/home passes.
All line tests consume fresh settled observations supplied by OpticalCycleMixin.
"""


class OpticalPatrolMixin:
    def _initialize_patrol(self):
        self._patrol_direction = 0
        self._patrol_first_box = None
        self._patrol_passes = 0
        self._patrol_done = False
        self._patrol_done_reason = None
        self._patrol_returning = False
        self._patrol_follow_started = False
        self._patrol_overview_since = None
        self._patrol_candidate = None
        self._patrol_last_detail_match = None
        self._patrol_entry_hits = 0
        self._patrol_entry_last = None

    def _patrol_event(self, event, timestamp, **values):
        self.catalog[self.active["id"]].setdefault("patrol_events", []).append({
            "event": event, "source_seconds": timestamp, "pass": self._patrol_passes,
            "direction": self._patrol_direction, **values})

    def _patrol_status(self):
        policy = self._cycle_policy or self.settings
        stage = self._patrol_done_reason or "waiting_overview_line"
        if self._patrol_returning:
            stage = "returning_overview"
        elif self.phase in {"zoomed", "panning", "zooming_in"}:
            stage = ("waiting_detail_entry" if not self._zoomed_ship_seen else
                     "detail_limit" if self._pan_limited else
                     "detail_step_limit" if self._pan_steps >= policy.pan_max_steps else
                     "detail_follow" if self._patrol_follow_started else "waiting_detail_stern")
        elif not self._patrol_direction:
            stage = "waiting_direction"
        return {"enabled": policy.line_patrol_enabled,
                "direction": {1: "left_to_right", -1: "right_to_left"}.get(self._patrol_direction),
                "pass_count": self._patrol_passes, "repeat_finished": self._patrol_done,
                "stage": stage, "overview_lines": [policy.overview_line_left, policy.overview_line_right],
                "detail_lines": [policy.detail_line_left, policy.detail_line_right],
                "left_limit": policy.pan_max_left_travel_ratio, "right_limit": policy.pan_max_right_travel_ratio}

    def _patrol_return(self, timestamp, reason):
        # Keep the same recording open through the return and next enlargement.
        self._flush_optical_media(timestamp)
        self._patrol_event("return_overview", timestamp, reason=reason)
        self._patrol_returning = True
        self._command_zoom(1., "zooming_out")

    def _patrol_overview_returned(self):
        self._patrol_returning = False
        self._active_zoom_factor = 1.
        self._patrol_overview_since = None
        self._patrol_follow_started = False
        self._zoomed_ship_seen = False
        self._zoomed_since = None
        self._empty_since = self._association_lost_since = None
        self._reacquire_candidate = None
        self._patrol_candidate = None
        self._patrol_entry_hits = 0
        self._patrol_entry_last = None
        self._phase("waiting_body")

    def _find_patrol_target(self, image, timestamp):
        if self._locked_appearance is None:
            competitors = [candidate for candidate in self.adaptive_selection["targets"]
                if self._consistent_locked_target(candidate, allow_ambiguous=True)
                and .6 <= candidate["height_ratio"] / max(.001, self._initial_target_height) <= 1.8]
            if len(competitors) > 1:
                self._patrol_candidate = None
                self._association_uncertain = True
                return None
        target = self._find_locked_target(timestamp)
        if target:
            self._patrol_candidate = None
            return target
        # A bow intentionally outside the enlarged view has no overlapping
        # old box. Confirm a unique arriving hull over three fresh frames.
        # This also handles an expanded full hull after returning home.
        matches = []
        zoom = float(self.current_zoom_factor or 1.)
        previous = self.locked_target["box"]
        pending = self._patrol_candidate
        if pending and pending["generation"] != self._cycle_generation:
            pending = None
        last_match = self._patrol_last_detail_match
        recent_view = (last_match is not None and last_match[0] == self._cycle_generation
                       and 0 < timestamp - last_match[1] <= self.target_selector.MAX_OBSERVATION_GAP)
        for candidate in self.adaptive_selection["targets"]:
            if not self._consistent_locked_target(candidate, check_appearance=False):
                continue
            ratio = candidate["height_ratio"] / max(.001, self._initial_target_height)
            if not .6 <= ratio <= 1.8:
                continue
            track = self.target_selector.tracks[candidate["target_id"]]
            similarity = self.target_selector.appearance_similarity(self._locked_appearance, track.appearance)
            box = self._source_target_box(track.vessel, image.shape[1], image.shape[0])
            if not box:
                continue
            known = candidate["ship_id"] == self.locked_target.get("ship_id") or similarity is not None
            entering = box[0] <= .05 if self._patrol_direction > 0 else box[2] >= .95
            old_overlap = max(0., min(previous[2], candidate["box"][2]) - max(previous[0], candidate["box"][0]))
            continuing = pending and pending["target_id"] == candidate["target_id"]
            same_track = (candidate["ship_id"] == self.locked_target.get("ship_id")
                          and track.vessel.get("tracked") is not False)
            adjacent = (zoom > 1 and same_track and recent_view
                        and candidate["target_id"] == self.locked_target["target_id"]
                        and self._geometric_reacquisition(candidate, check_appearance=False))
            # A cropped bow and the overview hull can have very different colors.
            # Admit a weak color match only with a tracked entry or same-view continuity.
            if (similarity is not None and similarity < .65
                    and not (zoom > 1 and same_track and (entering or continuing or adjacent))):
                continue
            if zoom > 1:
                forward = ((candidate["box"][0] + candidate["box"][2] - previous[0] - previous[2]) * self._patrol_direction >= -.04)
                plausible = adjacent or (forward and (known or entering or continuing))
            else:
                plausible = known or old_overlap > 0
            if plausible:
                matches.append((candidate, adjacent))
        if len(matches) != 1:
            self._patrol_candidate = None
            return None
        candidate, adjacent = matches[0]
        if adjacent:
            self._patrol_candidate = None
            self._association_uncertain = False
            return candidate
        if not pending or pending["target_id"] != candidate["target_id"] or timestamp - pending["last"] > 2.5:
            pending = {"target_id": candidate["target_id"], "hits": 0, "since": timestamp, "generation": self._cycle_generation}
        pending.update(hits=pending["hits"] + 1, last=timestamp)
        self._patrol_candidate = pending
        if pending["hits"] >= 3 and timestamp - pending["since"] >= .3:
            self._patrol_candidate = None
            self._association_uncertain = False
            return candidate
        return None

    def _patrol_observe_direction(self, box):
        if self._patrol_direction:
            return
        if self._patrol_first_box is None:
            self._patrol_first_box = box
        first = self._patrol_first_box
        # The entering edge identifies the bow, including a cropped long hull.
        if first[0] <= .02 and first[2] < .98:
            self._patrol_direction = 1
        elif first[2] >= .98 and first[0] > .02:
            self._patrol_direction = -1
        else:
            shift = (box[0] + box[2] - first[0] - first[2]) / 2
            if abs(shift) >= .01:
                self._patrol_direction = 1 if shift > 0 else -1

    def _process_patrol_observation(self, image, vessels, observation_context, timestamp, policy):
        if self.phase == "zoomed" and self._zoomed_since is None:
            self._zoomed_since = timestamp
        self._zoom_settled_pending = False
        target = self._find_patrol_target(image, timestamp)
        if target:
            self.locked_target = {**self.locked_target, **target}
            vessel = self.target_selector.tracks[target["target_id"]].vessel
            self._observe_media_target(vessel, image, timestamp)
            self._record_optical_attributes([vessel])
            self._empty_since = self._association_lost_since = None
            self._empty_hits = 0
            self._zoomed_ship_seen = True
            if self.phase == "zoomed":
                self._patrol_last_detail_match = (self._cycle_generation, timestamp)
                self.catalog[self.active["id"]]["target_reacquired"] = True
                if self._zoom_hold_since is None:
                    self._zoom_hold_since = timestamp
                self._maybe_follow_horizontal(vessel, image, timestamp, policy, observation_context=observation_context)
                return

            # At 1x + restored original pan, use only the overview plane.
            box = self._source_target_box(vessel, image.shape[1], image.shape[0])
            if not box:
                return
            self._patrol_observe_direction(box)
            if not self._patrol_direction:
                self._zoom_wait_reason = "waiting_direction"
                return
            rightward = self._patrol_direction > 0
            tail, bow = (box[0], box[2]) if rightward else (box[2], box[0])
            exit_line = policy.overview_line_right if rightward else policy.overview_line_left
            past_exit = tail >= exit_line if rightward else tail <= exit_line
            if past_exit:
                if not self._patrol_done:
                    self._patrol_event("no_more_zoom", timestamp, tail=tail)
                self._patrol_done = True
                self._patrol_done_reason = "overview_exit"
            if self._patrol_done:
                self._zoom_wait_reason = self._patrol_done_reason
                return
            entry_line = policy.overview_line_left if rightward else policy.overview_line_right
            before_entry = bow < entry_line if rightward else bow > entry_line
            if before_entry:
                self._patrol_overview_since = None
                self._patrol_entry_hits = 0
                self._patrol_entry_last = None
                self._zoom_wait_reason = "waiting_overview_line"
                return
            observed_at = (observation_context or {}).get("source_timestamp", timestamp)
            if self._patrol_entry_last is not None and observed_at <= self._patrol_entry_last:
                return
            self._patrol_entry_last = observed_at
            self._patrol_entry_hits += 1
            if self._patrol_overview_since is None:
                self._patrol_overview_since = observed_at
            if self._patrol_entry_hits < 2 or observed_at - self._patrol_overview_since < policy.overview_settle_seconds:
                self._zoom_wait_reason = "confirming_overview_line"
                return
            plan = self.target_selector.plan_zoom(target, policy, self.available_magnifications())
            if plan["zoom_factor"] <= 1:
                self._zoom_wait_reason = plan["selection_reason"]
                return
            self._patrol_passes += 1
            self._pan_limited = False
            self._last_pan_adjust = None
            self._patrol_follow_started = False
            self._active_zoom_factor = plan["zoom_factor"]
            item = self.catalog[self.active["id"]]
            item.update(optical_zoom_factor=self._active_zoom_factor, waiting_full_body=False, returned_to_overview=False,
                        selection_reason=plan["selection_reason"], requested_zoom_factor=plan["requested_zoom_factor"])
            if self._patrol_passes == 1:
                item["initial_optical_zoom_factor"] = self._active_zoom_factor
            self._patrol_event("zoom_in", timestamp, bow=bow, tail=tail, entry_line=entry_line,
                               entry_confirm_hits=self._patrol_entry_hits, observation_source_seconds=observed_at, **plan)
            self._command_zoom(self._active_zoom_factor, "zooming_in")
            return

        if self.phase == "waiting_body":
            self._patrol_overview_since = None
            self._patrol_entry_hits = 0
            self._patrol_entry_last = None
        # Ambiguous visible hulls retain footage but cannot dispatch a movement.
        positive = self._visible_detail_target(image, vessels, observation_context)
        if positive:
            self._observe_media_target(positive, image, timestamp)
            self._empty_since = None
            self._empty_hits = 0
            return
        if self.phase == "zoomed" and not self._zoomed_ship_seen:
            if self._zoomed_since is None:
                self._zoomed_since = timestamp
            if timestamp - self._zoomed_since >= policy.target_acquire_timeout_seconds:
                self._patrol_done = True
                self._patrol_done_reason = "overview_fallback"
                self._patrol_return(timestamp, "target_not_reacquired")
            return
        if self._hold_zoom(timestamp, policy):
            self._empty_since = None
            self._empty_hits = 0
            return
        self._empty_hits += 1
        if self._empty_since is None:
            self._empty_since = timestamp
        if timestamp - self._empty_since < policy.departure_confirm_seconds or self._empty_hits < policy.departure_min_observations:
            return
        if self.phase == "zoomed":
            self._patrol_return(timestamp, "detail_stern_left")
        else:
            self._patrol_event("overview_stern_left", timestamp)
            self._seal_optical_media(timestamp, "vessel_left")
            self._command_zoom(1., "zooming_out")
