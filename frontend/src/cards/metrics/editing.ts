import { createContext, useContext } from "react";

/** True while the dashboard is in edit mode: a card's name then isn't a link (cards are dragged). */
export const CardEditingContext = createContext(false);

export const useCardEditing = (): boolean => useContext(CardEditingContext);
