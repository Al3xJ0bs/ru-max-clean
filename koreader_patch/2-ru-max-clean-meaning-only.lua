-- RU Max Clean: keep the dictionary popup definition-only.
-- KOReader normally appends a separator and "(query : <word>)" to the first
-- dictionary result. RU Max Clean already shows the looked-up/result word in
-- the popup header, so this extra line is redundant for this reading setup.

local DictQuickLookup = require("ui/widget/dictquicklookup")

DictQuickLookup.addQueryWordToResult = function(self)
    -- Intentionally empty.
end
